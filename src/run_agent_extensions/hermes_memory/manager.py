"""Provider orchestration: fan-out, recall injection, commit queue.

Ported from hermes-agent's ``agent/memory_manager.py``. The manager is the single
integration point for memory providers in this distribution:

- at most one non-builtin provider is accepted (tool-schema bloat and conflicting
  backends are prevented at registration time);
- every fan-out call isolates provider failures, so one broken backend can never
  break a turn or another provider;
- provider writes run on ONE background worker, so turn N lands before turn N+1
  and a session boundary can serialize ``on_session_end`` strictly before
  ``on_session_switch``;
- shutdown drains that worker for a bounded window and reports what it abandoned
  instead of dropping work silently.

Usage from the extension::

    manager = MemoryManager()
    manager.add_provider(builtin_provider)
    manager.initialize_all(session_id, home=str(paths.home))

    prompt_block = manager.build_system_prompt()
    context = manager.prefetch_all(user_message, session_id=session_id)
    manager.sync_all(user_message, assistant_response, session_id=session_id)
    manager.queue_prefetch_all(user_message, session_id=session_id)
"""

from __future__ import annotations

import contextvars
import inspect
import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Literal

from run_agent_core.messages import AgentMessage

from .provider import MemoryProvider, RecallStatus, normalize_tool_schema

logger = logging.getLogger(__name__)

# How long ``shutdown_all`` waits for in-flight background sync/prefetch work to
# drain before abandoning it. A wedged provider must never block teardown
# indefinitely — anything still running past this window is reported, and its
# queued siblings are cancelled.
DEFAULT_SYNC_DRAIN_TIMEOUT_S = 5.0
# How long one non-builtin provider's ``prefetch`` may block the turn before the
# manager gives up on it for this turn (and until the stuck call returns).
DEFAULT_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0

# Tool names the distribution reserves: the built-in coding tools plus the name of
# the built-in memory surface itself. A provider tool that shadows one of these is
# rejected at registration — built-ins always win, so a shadowed provider tool
# would never be routed and would only linger in the routing table.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {"read", "write", "edit", "bash", "grep", "find", "ls", "memory"}
)

DrainStatus = Literal["drained", "timed_out"]

# Background work is classified so a timed-out drain can report writes and
# prefetches separately: an abandoned write loses data, an abandoned prefetch
# only loses a cached recall.
BackgroundKind = Literal["write", "prefetch"]


@dataclass(frozen=True, slots=True)
class FlushResult:
    """Explicit outcome of a bounded drain of the background worker.

    ``status`` is ``"drained"`` when every queued task finished (or was already
    cancelled) inside the window and ``"timed_out"`` when work had to be
    abandoned. ``abandoned_writes``/``abandoned_prefetches`` count the queued
    tasks cancelled by that timeout; ``active_tasks`` counts tasks that were
    already running and could not be cancelled.
    """

    status: DrainStatus = "drained"
    abandoned_writes: int = 0
    abandoned_prefetches: int = 0
    active_tasks: int = 0


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always accepted. Only one non-builtin (external)
    provider is allowed: a second one is rejected with a warning. Failures in one
    provider never block the other, and never propagate into the caller.
    """

    def __init__(
        self,
        *,
        external_prefetch_timeout: float | None = None,
        drain_timeout: float | None = None,
    ) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}
        self._has_external = False
        self._external_prefetch_timeout = (
            DEFAULT_EXTERNAL_PREFETCH_TIMEOUT_S
            if external_prefetch_timeout is None
            else float(external_prefetch_timeout)
        )
        if self._external_prefetch_timeout <= 0:
            raise ValueError("external_prefetch_timeout must be positive")
        self._drain_timeout = (
            DEFAULT_SYNC_DRAIN_TIMEOUT_S if drain_timeout is None else float(drain_timeout)
        )
        if self._drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        self._external_prefetch_threads: dict[str, threading.Thread] = {}
        self._external_prefetch_lock = threading.Lock()
        # Statuses captured after the most recent prefetch_all, in provider order.
        self._recall_statuses: list[RecallStatus] = []
        # Lazily created single worker: the common builtin-only path spawns no
        # extra threads, and a provider's write ordering is guaranteed once it
        # exists (single worker = FIFO).
        self._sync_executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        self._background_futures: dict[Future[None], BackgroundKind] = {}
        self._submit_serial = 0
        self._last_flush: tuple[int, FlushResult] | None = None
        self._shutting_down = False

    # -- Registration --------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider.

        Built-in provider (name ``"builtin"``) is always accepted. Only ONE
        external (non-builtin) provider is allowed — a second attempt is rejected
        with a warning. Provider tools that shadow :data:`RESERVED_TOOL_NAMES` are
        rejected individually; the provider itself stays registered.
        """
        is_builtin = provider.name == "builtin"

        if not is_builtin:
            if self._has_external:
                existing = next((p.name for p in self._providers if p.name != "builtin"), "unknown")
                logger.warning(
                    "Rejected memory provider '%s' — external provider '%s' is already "
                    "registered. Only one external memory provider is allowed at a time.",
                    provider.name,
                    existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)

        try:
            schemas = provider.get_tool_schemas()
        except Exception as exc:
            logger.warning("Memory provider '%s' get_tool_schemas() failed: %s", provider.name, exc)
            return

        for raw_schema in schemas:
            schema = normalize_tool_schema(raw_schema)
            if schema is None:
                logger.warning(
                    "Memory provider '%s' returned a tool schema with no resolvable name; "
                    "skipping (%r)",
                    provider.name,
                    raw_schema,
                )
                continue
            tool_name = str(schema["name"])
            if tool_name in RESERVED_TOOL_NAMES:
                logger.warning(
                    "Memory provider '%s' tool '%s' shadows a reserved tool name; "
                    "registration ignored. Built-in tools always win — rename the "
                    "provider's tool to something unique.",
                    provider.name,
                    tool_name,
                )
                continue
            if tool_name in self._tool_to_provider:
                logger.warning(
                    "Memory tool name conflict: '%s' already registered by %s, ignoring from %s",
                    tool_name,
                    self._tool_to_provider[tool_name].name,
                    provider.name,
                )
                continue
            self._tool_to_provider[tool_name] = provider

        logger.info("Memory provider '%s' registered (%d tools)", provider.name, len(schemas))

    @property
    def providers(self) -> tuple[MemoryProvider, ...]:
        """All registered providers in registration order."""
        return tuple(self._providers)

    def get_provider(self, name: str) -> MemoryProvider | None:
        """Return a registered provider by name, or None."""
        for provider in self._providers:
            if provider.name == name:
                return provider
        return None

    def initialize_all(self, session_id: str, **kwargs: object) -> None:
        """Initialize every provider for a session, isolating failures.

        The caller owns profile-scoped paths: pass ``home`` (the active Run Agent
        home directory) so a provider never hardcodes ``~/.run``.
        """
        for provider in self._providers:
            try:
                provider.initialize(session_id, **kwargs)
            except Exception as exc:
                logger.warning("Memory provider '%s' initialize failed: %s", provider.name, exc)

    # -- System prompt -------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Collect the static system-prompt blocks from all providers.

        Returns the combined text, or an empty string when no provider
        contributes. Each block is separated by a blank line; a failing provider
        is skipped with a warning.
        """
        blocks: list[str] = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
            except Exception as exc:
                logger.warning(
                    "Memory provider '%s' system_prompt_block() failed: %s", provider.name, exc
                )
                continue
            if block and block.strip():
                blocks.append(block)
        return "\n\n".join(blocks)

    # -- Prefetch / recall ---------------------------------------------------

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """Collect prefetch context from all providers for the upcoming turn.

        Every provider runs at most once, empty results are skipped, and a failing
        provider is isolated: it neither blocks the others nor the caller. The
        per-provider :class:`RecallStatus` of this call replaces the previous one.
        """
        self._recall_statuses = []
        parts: list[str] = []
        for provider in self._providers:
            try:
                result = self._prefetch_provider(provider, query, session_id=session_id)
            except Exception as exc:
                logger.debug(
                    "Memory provider '%s' prefetch failed (non-fatal): %s", provider.name, exc
                )
                self._capture_recall_status(provider)
                continue
            if result and result.strip():
                parts.append(result)
            self._capture_recall_status(provider)
        return "\n\n".join(parts)

    def _capture_recall_status(self, provider: MemoryProvider) -> None:
        """Record what ``provider``'s LAST prefetch injected, for the indicator."""
        try:
            status = provider.recall_status()
        except Exception as exc:
            logger.debug(
                "Memory provider '%s' recall_status failed (non-fatal): %s", provider.name, exc
            )
            return
        if status is not None:
            self._recall_statuses.append(status)

    def _prefetch_provider(
        self, provider: MemoryProvider, query: str, *, session_id: str = ""
    ) -> str:
        """Run one provider's prefetch, bounding a non-builtin provider's latency.

        The built-in file provider is called inline because it only formats data
        it already holds. An external provider may block on the network or a
        daemon, so its call runs on a dedicated thread with a timeout; a call that
        is still running when the next turn arrives is skipped rather than piled
        on, so one wedged backend cannot accumulate threads.
        """
        if provider.name == "builtin":
            return provider.prefetch(query, session_id=session_id) or ""

        result_box: dict[str, str] = {}
        error_box: dict[str, Exception] = {}

        def _run() -> None:
            try:
                result_box["value"] = provider.prefetch(query, session_id=session_id) or ""
            except Exception as exc:  # re-raised by the caller, on the turn thread
                error_box["value"] = exc

        # Propagate the caller's contextvars: extension state (for example the
        # learning-writeback gate) is ContextVar-scoped, and a fresh thread starts
        # with an empty context.
        context = contextvars.copy_context()

        def propagate() -> None:
            context.run(_run)

        thread = threading.Thread(
            target=propagate,
            daemon=True,
            name=f"memory-prefetch-{provider.name}",
        )
        with self._external_prefetch_lock:
            existing = self._external_prefetch_threads.get(provider.name)
            if existing is not None:
                if existing.is_alive():
                    logger.debug(
                        "Memory provider '%s' prefetch is still running; skipping this turn",
                        provider.name,
                    )
                    return ""
                self._external_prefetch_threads.pop(provider.name, None)
            self._external_prefetch_threads[provider.name] = thread
            thread.start()

        thread.join(self._external_prefetch_timeout)
        if thread.is_alive():
            logger.warning(
                "Memory provider '%s' prefetch timed out after %.1fs; skipping it until the "
                "stuck call returns",
                provider.name,
                self._external_prefetch_timeout,
            )
            return ""

        with self._external_prefetch_lock:
            if self._external_prefetch_threads.get(provider.name) is thread:
                self._external_prefetch_threads.pop(provider.name, None)
        if error_box:
            raise error_box["value"]
        return result_box.get("value", "")

    def recall_status(self) -> RecallStatus | None:
        """Return the most recent recall status, or None when nothing was injected.

        Reflects only the LAST :meth:`prefetch_all` — never a stale prior count.
        """
        return self._recall_statuses[-1] if self._recall_statuses else None

    def recall_statuses(self) -> tuple[RecallStatus, ...]:
        """Every status captured by the most recent :meth:`prefetch_all`, in order."""
        return tuple(self._recall_statuses)

    def describe_recall(self, status: RecallStatus | None = None) -> str:
        """Build a deterministic, model-independent recall indicator line.

        Renders ``"🧠 Provider — recalled N memories"`` (singular for one,
        ``"recalled relevant memory"`` when a provider injected content without a
        discrete count). With no argument every status from the last
        :meth:`prefetch_all` is rendered; an explicit ``status`` renders just that
        one. Returns ``""`` when there is nothing to report, so callers can emit
        the result unconditionally.
        """
        statuses = (status,) if status is not None else self.recall_statuses()
        segments: list[str] = []
        for item in statuses:
            if item is None:
                continue
            if item.count == 1:
                detail = "recalled 1 memory"
            elif item.count > 1:
                detail = f"recalled {item.count} memories"
            else:
                # count <= 0 → content injected but no discrete count.
                detail = "recalled relevant memory"
            segments.append(f"{item.glyph} {item.provider_label} — {detail}")
        return "  ".join(segments)

    def queue_prefetch_all(self, query: str, *, session_id: str = "") -> None:
        """Queue background recall on all providers for the NEXT turn.

        Dispatched to the background worker so a slow or wedged provider can never
        block the caller; the result is consumed by ``prefetch()`` on the next
        turn.
        """
        providers = list(self._providers)
        if not providers:
            return
        if not query or not query.strip():
            return

        def _run() -> None:
            for provider in providers:
                try:
                    provider.queue_prefetch(query, session_id=session_id)
                except Exception as exc:
                    logger.debug(
                        "Memory provider '%s' queue_prefetch failed (non-fatal): %s",
                        provider.name,
                        exc,
                    )

        self._submit_background(_run, kind="prefetch")

    # -- Sync ----------------------------------------------------------------

    @staticmethod
    def _provider_sync_accepts_messages(provider: MemoryProvider) -> bool:
        """Return whether ``sync_turn`` accepts a ``messages`` keyword."""
        try:
            signature = inspect.signature(provider.sync_turn)
        except (TypeError, ValueError):
            return True
        parameters = list(signature.parameters.values())
        if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
            return True
        return "messages" in signature.parameters

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Sequence[AgentMessage] | None = None,
    ) -> None:
        """Sync a completed turn to all providers.

        Runs on the background worker, NOT inline on the turn-completion path: a
        provider's ``sync_turn`` may block on a network or daemon call, and doing
        that inline would hold the run open long after the user saw their reply
        (every frontend would keep the agent marked "running"). Writes are
        serialized through one worker, so turn N lands before turn N+1.
        """
        providers = list(self._providers)
        if not providers:
            return
        if not user_content or not user_content.strip():
            return

        def _run() -> None:
            for provider in providers:
                try:
                    if messages is not None and self._provider_sync_accepts_messages(provider):
                        provider.sync_turn(
                            user_content,
                            assistant_content,
                            session_id=session_id,
                            messages=messages,
                        )
                    else:
                        provider.sync_turn(user_content, assistant_content, session_id=session_id)
                except Exception as exc:
                    logger.warning("Memory provider '%s' sync_turn failed: %s", provider.name, exc)

        self._submit_background(_run, kind="write")

    # -- Background dispatch -------------------------------------------------

    def _submit_background(self, fn: Callable[[], None], *, kind: BackgroundKind = "write") -> None:
        """Queue ``fn`` on the serialized worker and track its durability class.

        The callable is wrapped with the CALLER's contextvars: extension state is
        ContextVar-scoped (the learning-writeback gate in
        ``run_agent_coding.host.learning`` is the important one here) and executor
        threads start with an empty context, so a write must inherit the context
        that requested it.
        """
        context = contextvars.copy_context()

        def wrapped() -> None:
            context.run(fn)

        executor = self._get_sync_executor()
        if executor is None:
            if self._shutting_down:
                logger.warning("Memory manager is shutting down; rejecting late %s task", kind)
                return
            # Creation failure outside shutdown: run inline rather than lose the
            # write (slow, but correct).
            try:
                wrapped()
            except Exception as exc:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", exc)
            return
        try:
            with self._executor_lock:
                if self._shutting_down:
                    logger.warning("Memory manager is shutting down; rejecting late %s task", kind)
                    return
                future = executor.submit(wrapped)
                self._background_futures[future] = kind
                self._submit_serial += 1
            future.add_done_callback(self._forget_background_future)
        except RuntimeError:
            if self._shutting_down:
                logger.warning("Memory manager shut down during %s submission; task rejected", kind)
                return
            try:
                wrapped()
            except Exception as exc:  # pragma: no cover - fn guards internally
                logger.debug("Inline memory background task failed: %s", exc)

    def _forget_background_future(self, future: Future[None]) -> None:
        with self._executor_lock:
            self._background_futures.pop(future, None)

    def _get_sync_executor(self) -> ThreadPoolExecutor | None:
        """Lazily create the single-worker background executor."""
        if self._shutting_down:
            return None
        if self._sync_executor is not None:
            return self._sync_executor
        with self._executor_lock:
            if self._shutting_down:
                return None
            if self._sync_executor is None:
                try:
                    self._sync_executor = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="memory-sync"
                    )
                except Exception as exc:  # pragma: no cover - resource exhaustion
                    logger.warning("Failed to create memory sync executor: %s", exc)
                    return None
            return self._sync_executor

    def flush_pending(self, timeout: float | None = None) -> FlushResult:
        """Block until queued sync/prefetch work has drained, bounded by ``timeout``.

        A single worker means submitting a barrier and waiting on it guarantees
        every previously-submitted task has run, in FIFO order. On timeout the
        queued (not yet started) tasks are cancelled and reported as abandoned
        instead of being dropped silently.

        Repeated calls are idempotent: a call made with no new submission in
        between returns the recorded outcome rather than re-counting (or
        re-cancelling) the same work.
        """
        effective = self._drain_timeout if timeout is None else float(timeout)
        if effective <= 0:
            raise ValueError("flush timeout must be positive")
        with self._executor_lock:
            serial = self._submit_serial
            executor = self._sync_executor
        if self._last_flush is not None and self._last_flush[0] == serial:
            return self._last_flush[1]
        if executor is None:
            return self._remember_flush(serial, FlushResult())
        try:
            barrier = executor.submit(lambda: None)
        except RuntimeError:
            # Executor already shut down — nothing pending.
            return self._remember_flush(serial, FlushResult())
        try:
            barrier.result(timeout=effective)
        except Exception:
            return self._remember_flush(serial, self._abandon_pending(effective))
        return self._remember_flush(serial, FlushResult())

    def _remember_flush(self, serial: int, result: FlushResult) -> FlushResult:
        self._last_flush = (serial, result)
        return result

    def _abandon_pending(self, waited: float) -> FlushResult:
        """Report what the barrier timed out on: cancel queued work, count the rest."""
        with self._executor_lock:
            tracked = tuple(self._background_futures)
        return self._drain_tracked(tracked, 0.0, reported=waited)

    def shutdown_all(self, *, timeout: float | None = None) -> FlushResult:
        """Drain queued work, then shut every provider down in reverse order.

        The drain waits on the futures that were queued when shutdown began, for a
        bounded window, and reports explicitly what it abandoned (cancelled) versus
        what is still running detached. ``executor.shutdown(wait=False,
        cancel_futures=False)`` closes submission without touching the FIFO, so a
        turn's final write still lands if it fits the window.

        Safe to call repeatedly: submissions after the first call are rejected with
        a warning, later calls drain nothing, and provider ``shutdown()`` must itself
        tolerate a second call.
        """
        effective = self._drain_timeout if timeout is None else float(timeout)
        if effective <= 0:
            raise ValueError("shutdown timeout must be positive")
        with self._executor_lock:
            self._shutting_down = True
            executor = self._sync_executor
            self._sync_executor = None
            tracked = tuple(self._background_futures)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=False)
        result = self._drain_tracked(tracked, effective)
        with self._executor_lock:
            self._last_flush = (self._submit_serial, result)
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning("Memory provider '%s' shutdown failed: %s", provider.name, exc)
        return result

    def _drain_tracked(
        self,
        tracked: tuple[Future[None], ...],
        timeout: float,
        *,
        reported: float | None = None,
    ) -> FlushResult:
        """Wait a bounded window for already-queued work, then report the leftovers.

        ``reported`` overrides the window named in the timeout warning: the barrier
        path waits no further (its deadline already elapsed) but should still report
        how long it waited.
        """
        if not tracked:
            return FlushResult()
        _, pending = wait(tracked, timeout=timeout)
        if not pending:
            return FlushResult()
        with self._executor_lock:
            kinds = {future: self._background_futures.get(future, "write") for future in pending}
        abandoned_writes = 0
        abandoned_prefetches = 0
        active_tasks = 0
        for future in pending:
            if future.cancel():
                if kinds[future] == "prefetch":
                    abandoned_prefetches += 1
                else:
                    abandoned_writes += 1
            else:
                active_tasks += 1
        logger.warning(
            "Memory drain timed out after %.2fs; abandoning %d queued write(s) and "
            "%d queued prefetch(es); %d active task(s) remain detached",
            timeout if reported is None else reported,
            abandoned_writes,
            abandoned_prefetches,
            active_tasks,
        )
        return FlushResult(
            status="timed_out",
            abandoned_writes=abandoned_writes,
            abandoned_prefetches=abandoned_prefetches,
            active_tasks=active_tasks,
        )

    # -- Tools ---------------------------------------------------------------

    def get_all_tool_schemas(self) -> list[dict[str, object]]:
        """Collect tool schemas from all providers.

        Reserved tool names are skipped — they were rejected from the routing
        table in :meth:`add_provider`, so the manager must not advertise a schema
        it will never route. A provider whose ``get_tool_schemas()`` raises is
        skipped with a warning, and a nameless schema is dropped rather than
        poisoning the whole request.
        """
        schemas: list[dict[str, object]] = []
        seen: set[str] = set()
        for provider in self._providers:
            try:
                raw_schemas = provider.get_tool_schemas()
            except Exception as exc:
                logger.warning(
                    "Memory provider '%s' get_tool_schemas() failed: %s", provider.name, exc
                )
                continue
            for raw_schema in raw_schemas:
                schema = normalize_tool_schema(raw_schema)
                if schema is None:
                    logger.warning(
                        "Memory provider '%s' returned a tool schema with no resolvable name; "
                        "skipping (%r)",
                        provider.name,
                        raw_schema,
                    )
                    continue
                name = str(schema["name"])
                if name in RESERVED_TOOL_NAMES or name in seen:
                    continue
                schemas.append(schema)
                seen.add(name)
        return schemas

    def get_all_tool_names(self) -> set[str]:
        """Return every tool name the manager will route."""
        return set(self._tool_to_provider)

    def has_tool(self, tool_name: str) -> bool:
        """Return whether any provider handles this tool."""
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: Mapping[str, object], **kwargs: object) -> str:
        """Route a tool call to the provider that owns it.

        Returns a JSON string. An unrouted name or a failing provider becomes a
        JSON error result rather than an exception into the agent loop.
        """
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return _tool_error(f"No memory provider handles tool '{tool_name}'")
        try:
            return provider.handle_tool_call(tool_name, args, **kwargs)
        except Exception as exc:
            logger.error(
                "Memory provider '%s' handle_tool_call(%s) failed: %s",
                provider.name,
                tool_name,
                exc,
            )
            return _tool_error(f"Memory tool '{tool_name}' failed: {exc}")

    # -- Lifecycle hooks -----------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: object) -> None:
        """Notify all providers of a new turn (before prefetch)."""
        for provider in self._providers:
            try:
                provider.on_turn_start(turn_number, message, **kwargs)
            except Exception as exc:
                logger.debug("Memory provider '%s' on_turn_start failed: %s", provider.name, exc)

    def on_session_end(self, messages: Sequence[AgentMessage]) -> None:
        """Notify all providers that the session ended.

        Per-provider failures are isolated, so this never raises and never
        suppresses the ``on_session_switch`` half of a session boundary.
        """
        for provider in self._providers:
            try:
                provider.on_session_end(messages)
            except Exception as exc:
                logger.warning("Memory provider '%s' on_session_end failed: %s", provider.name, exc)

    def commit_session_boundary_async(
        self,
        messages: Sequence[AgentMessage],
        *,
        new_session_id: str,
        parent_session_id: str = "",
        reason: str = "new_session",
    ) -> None:
        """Queue old-session extraction plus provider rebinding as ONE task.

        Session rotation must deliver ``on_session_end`` (end-of-session
        extraction, potentially slow) strictly BEFORE ``on_session_switch`` (which
        rebinds provider-internal session state). Submitting both hooks as one task
        on the single worker gives both properties at one chokepoint: the caller
        returns immediately, and FIFO order serializes end→switch against every
        other provider write already queued. A late switch would otherwise run
        ``on_session_end`` against post-switch bindings and misattribute the old
        transcript to the new session.
        """
        if not self._providers:
            return
        snapshot = list(messages)

        def _run() -> None:
            self._run_session_boundary(snapshot, new_session_id, parent_session_id, reason)

        self._submit_background(_run, kind="write")

    def _run_session_boundary(
        self,
        messages: list[AgentMessage],
        new_session_id: str,
        parent_session_id: str,
        reason: str,
    ) -> None:
        """Run ``on_session_end`` then ``on_session_switch``, isolating both.

        The switch is fire-and-forget relative to the extraction: even when
        extraction raises, providers must still be rebound to the new session, or
        every later write would land in the previous session's record.
        """
        try:
            self.on_session_end(messages)
        except Exception as exc:  # pragma: no cover - per provider isolation below
            logger.warning("Session-boundary extraction failed (%s): %s", reason, exc)
        try:
            self.on_session_switch(
                new_session_id,
                parent_session_id=parent_session_id,
                reset=True,
            )
        except Exception as exc:  # pragma: no cover - per provider isolation below
            logger.warning("Session-boundary switch failed (%s): %s", reason, exc)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
    ) -> None:
        """Notify all providers that the session identity rotated.

        Providers keep running; they only need to refresh cached per-session state
        so subsequent writes land in the correct session record. ``rewound`` is
        forwarded only when set, so the common ``/resume`` and ``/new`` paths do
        not pollute providers that capture extra keyword arguments.
        """
        if not new_session_id:
            return
        for provider in self._providers:
            try:
                if rewound:
                    provider.on_session_switch(
                        new_session_id,
                        parent_session_id=parent_session_id,
                        reset=reset,
                        rewound=True,
                    )
                else:
                    provider.on_session_switch(
                        new_session_id, parent_session_id=parent_session_id, reset=reset
                    )
            except Exception as exc:
                logger.debug(
                    "Memory provider '%s' on_session_switch failed: %s", provider.name, exc
                )

    def on_pre_compress(self, messages: Sequence[AgentMessage]) -> str:
        """Collect pre-compression contributions from all providers."""
        parts: list[str] = []
        for provider in self._providers:
            try:
                result = provider.on_pre_compress(messages)
            except Exception as exc:
                logger.debug("Memory provider '%s' on_pre_compress failed: %s", provider.name, exc)
                continue
            if result and result.strip():
                parts.append(result)
        return "\n\n".join(parts)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Mirror a built-in memory write to every external provider.

        The builtin provider is skipped: it is the source of the write. Metadata
        carries the write provenance (origin, session, tool) so a mirrored fact
        stays auditable.
        """
        for provider in self._providers:
            if provider.name == "builtin":
                continue
            try:
                provider.on_memory_write(action, target, content, dict(metadata or {}))
            except Exception as exc:
                logger.debug("Memory provider '%s' on_memory_write failed: %s", provider.name, exc)

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs: object
    ) -> None:
        """Notify all providers that a subagent completed."""
        for provider in self._providers:
            try:
                provider.on_delegation(task, result, child_session_id=child_session_id, **kwargs)
            except Exception as exc:
                logger.debug("Memory provider '%s' on_delegation failed: %s", provider.name, exc)


def _tool_error(message: str) -> str:
    """Build the JSON error result the agent loop receives for a failed tool call."""
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


__all__ = [
    "DEFAULT_EXTERNAL_PREFETCH_TIMEOUT_S",
    "DEFAULT_SYNC_DRAIN_TIMEOUT_S",
    "RESERVED_TOOL_NAMES",
    "BackgroundKind",
    "DrainStatus",
    "FlushResult",
    "MemoryManager",
]
