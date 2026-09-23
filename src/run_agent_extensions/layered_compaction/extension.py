"""``setup(api)`` wiring: the cheap-first L1 → L2 → L3 → L4 request pipeline.

This extension owns the preparation of every Provider request it sees: the core
applies no compaction of its own, so the pipeline runs in
``before_provider_request`` only, in this order, and never mutates session
history (the transcript is untouched; only the request view changes):

``gate``
    the view is estimated first; at or below 80% of the budget *no layer runs at
    all* and the request is left exactly as it was. Over the gate the three free
    layers run as one batch and the view is measured again — only a view that is
    still over the gate goes on to L4.
``L1``
    a tool result whose text exceeds the character threshold is written to
    ``cwd/.run/tool-results/<tool_call_id>.txt`` and replaced by a preview that
    names the file.
``L2``
    a view longer than the message limit keeps a short head and a long tail with
    one placeholder for the middle.
``L3``
    every tool result except the most recent few, whose text is longer than the
    character floor, has its content replaced with a short placeholder.
``L4``
    the prefix before a user-boundary split is summarized through
    ``services.inference.complete`` (bounded by ``asyncio.wait_for``), and the
    core is asked to commit the result over ``CompactionCommitRequest``.

Every layer number here is the *execution* order; the reference implementation
numbers the same strategies ``L3``/``L1``/``L2``/``L4`` (see the package README).

At most one commit is requested per request (:meth:`LayeredCompaction._commit_for`).
The extension fires the `session_before_compact` hook itself
(``context.request_session_before_compact``) only for attempts that will really
try to commit: once every precondition for producing the summary holds (a
provider is available, the split point is legal, the breaker is not tripped) and
always before the summary model call, so a cancel both skips the commit and
saves that call. The gate's decision also carries what the subscribing memory
providers produced before this compaction (hermes-agent's ``on_pre_compress``):
that text is injected into the summarizer prompt as fenced *material* under
``<memory-provider-context>``, never as an instruction and never as the summary
itself. A cancel skips L4 for the request, keeps the free L1/L2/L3 rewrites, and
only says why with a warning. The core never fires that hook: compaction is the
extension's.

Two host seams shape the port and are documented in the README:

* the hook runs *inside* a foreground run, where the host refuses extension
  inference (``InferenceBusy``). The summary is therefore prepared at
  ``agent_settled`` (idle) or by ``/compact``, and an inline attempt is made when
  configured — its ``InferenceBusy`` defers without counting toward the circuit
  breaker.
* the API exposes no entry-id seam, so ``first_kept_entry_id`` is resolved from
  the ``context_entry_ids`` of the last recorded agent snapshot
  (:func:`state.active_entry_ids`).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import time
from typing import cast

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import (
    AfterProviderResponseEvent,
    BeforeAgentStartEvent,
    BeforeProviderRequestEvent,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
    SessionCompactEvent,
    SessionCompactFailedEvent,
    SessionStartEvent,
)
from run_agent_coding.extensions.api import (
    BeforeProviderRequestResult,
    CompactionCommitRequest,
    CompactionTrigger,
    SessionBeforeCompactDecision,
)
from run_agent_coding.host.inference import InferenceBusy, InferenceUnavailable
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AgentMessage, AssistantMessage, UserMessage
from run_agent_core.provider import ModelRequest

from .config import CompactionConfig, load_config, resolve_context_window, should_compact
from .layers import (
    estimate_message_tokens,
    estimate_request_tokens,
    run_free_layers,
)
from .state import (
    PreparedSummary,
    SessionState,
    active_entry_ids,
    legalize_view,
    message_key,
    persist_summary,
    resolve_first_kept_entry_id,
    summary_message_text,
    view_summary_text,
)
from .summary import (
    SummaryUnavailable,
    reactive_reason_for_error_text,
    reactive_reason_for_status,
    request_summary,
    select_cut_index,
)

COMPACT_COMMAND = "compact"
COMPACT_USAGE = "/compact [instructions]"
COMPACT_RESULT_TEMPLATE = (
    "Prepared a manual summary over {rows} message(s); it is committed on the next request."
)
DISABLED_NOTE = "the compaction extension is off for this session (COMPACTION_LAYER_ENABLED=0)"
TOOL_RESULTS_DIRECTORY = Path(".run") / "tool-results"
PERSISTED_OUTPUT_GUIDELINE = (
    "A tool result too large to keep in context is written to "
    f"`{TOOL_RESULTS_DIRECTORY.as_posix()}/<tool_call_id>.txt` and the conversation shows a "
    "`<persisted-output>` preview naming that file: read the file when the full output matters."
)


def _compaction_trigger(value: str) -> CompactionTrigger:
    """Narrow a stored trigger string onto the commit channel's literal."""
    if value == "manual":
        return "manual"
    if value == "reactive":
        return "reactive"
    return "auto"


class LayeredCompaction:
    """One loaded extension instance: per-session state plus the hook pipeline."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.state = SessionState()
        self.config = CompactionConfig()

    # ------------------------------------------------------------------ config
    def configure(self, context: ExtensionContext) -> CompactionConfig:
        """Load the environment configuration that owns this session's rewriting."""
        try:
            self.config = load_config(
                context.environment, model_window=context.context_window_tokens
            )
        except ValueError as exc:
            self.api.notify(f"compaction: invalid configuration ({exc})", "warning")
            self.config = CompactionConfig(enabled=False)
        self.state.config = self.config
        # Loading this extension is what gives a session compaction, so the only
        # switch left is the extension's own configuration.
        self.state.rewrite_enabled = self.config.enabled
        return self.config

    def _refresh_context_window(self, context: ExtensionContext) -> None:
        """Re-resolve the budget from the bound session, e.g. after a model switch.

        The number always comes from ``context.context_window_tokens`` (the same
        value the core's hard guard measures against), never from a local
        estimate; the explicit override can only tighten it, and the retained-tail
        budget follows the model window unless it was configured explicitly.
        """
        window = resolve_context_window(
            context.environment, model_window=context.context_window_tokens
        )
        if window == self.config.context_window:
            return
        environment = context.environment
        explicit_tail = environment.get("COMPACTION_LAYER_KEEP_RECENT_TOKENS")
        keep_recent_tokens = self.config.keep_recent_tokens
        if explicit_tail is None or not explicit_tail.strip():
            keep_recent_tokens = window // 4
        self.config = replace(
            self.config, context_window=window, keep_recent_tokens=keep_recent_tokens
        )
        self.state.config = self.config

    @property
    def rewrite_enabled(self) -> bool:
        """Return whether this session's configuration lets the layers rewrite."""
        return self.state.rewrite_enabled

    # ------------------------------------------------------------------- hooks

    async def on_session_start(self, event: object, context: ExtensionContext) -> None:
        """Load configuration and reset per-session state."""
        if not isinstance(event, SessionStartEvent):
            return
        self.state.reset_session()
        self.configure(context)

    async def on_before_agent_start(self, event: object, context: ExtensionContext) -> None:
        """Start-of-run reset: re-resolve the budget, then reset the reactive latch.

        ``event.system_prompt`` is observed but never rewritten: the system prompt
        stays part of the request the core built (the rewrite always carries
        ``request.system`` through unchanged).
        """
        if not isinstance(event, BeforeAgentStartEvent):
            return
        self.state.reset_run()
        if self.config.enabled:
            self._refresh_context_window(context)

    async def on_before_provider_request(
        self, event: object, context: ExtensionContext
    ) -> BeforeProviderRequestResult | None:
        """Run the gate, then L1 → L2 → L3 → L4, and return the view plus one commit."""
        if not isinstance(event, BeforeProviderRequestEvent):
            return None
        if not self.rewrite_enabled:
            return None

        self.state.begin_request()
        request = event.payload
        view = list(request.messages)
        self.state.last_view = tuple(view)
        tokens = estimate_request_tokens(request.system, view)
        self.state.last_request_tokens = tokens

        view, changed, commit = await self._prepare_request(context, request, view, tokens)
        if not changed and commit is None:
            return None
        replacement = None
        if changed:
            view = list(legalize_view(view))
            replacement = ModelRequest(
                model=request.model,
                system=request.system,
                messages=tuple(view),
                tools=request.tools,
                session_id=request.session_id,
            )
        return BeforeProviderRequestResult(request=replacement, compaction=commit)

    async def on_after_provider_response(self, event: object, context: ExtensionContext) -> None:
        """Record the HTTP status; a 413 is a definitive media-too-large signal."""
        if not isinstance(event, AfterProviderResponseEvent):
            return
        self.state.last_status = event.status
        if not (self.config.reactive_enabled and self.rewrite_enabled):
            return
        if reactive_reason_for_status(event.status) == "media_size":
            self.state.reactive_armed = True
            self.state.note("reactive armed: media_size (HTTP 413)")

    async def on_message_end(self, event: object, context: ExtensionContext) -> None:
        """Arm the reactive path from a precise provider error message."""
        if not isinstance(event, MessageEndEvent):
            return
        message = event.message
        if not isinstance(message, AssistantMessage) or message.stop_reason != "error":
            return
        if not (self.config.reactive_enabled and self.rewrite_enabled):
            return
        reason = reactive_reason_for_error_text(message.error_message or "")
        if reason is not None:
            self.state.reactive_armed = True
            self.state.note(f"reactive armed: {reason}")

    async def on_agent_settled(self, event: object, context: ExtensionContext) -> None:
        """Idle phase: refresh entry ids, then prepare an L4 summary if needed."""
        if not isinstance(event, AgentSettledEvent):
            return
        if not self.rewrite_enabled:
            return
        await self._refresh_entry_ids(context, event.snapshot_id)
        if not self.config.l4_enabled or self.state.breaker_tripped():
            return
        if self.state.prepared is not None:
            return
        view = list(self.state.last_view)
        if not view:
            return
        # The idle phase is where the summary model call can actually run: a
        # foreground run owns the provider while a request is in flight.
        if not should_compact(self.state.last_request_tokens, self.config):
            return
        await self._prepare_summary(
            context,
            view,
            trigger="auto",
            count_failures=True,
            record=True,
        )

    async def on_session_compact(self, event: object, context: ExtensionContext) -> None:
        """Reconcile after a commit: the durable head is the summary now."""
        if not isinstance(event, SessionCompactEvent) or not event.from_extension:
            return
        self.state.consume_prepared()
        self.state.note_success()
        self.state.entry_ids = ()
        self.state.entry_ids_snapshot = None

    async def on_session_compact_failed(self, event: object, context: ExtensionContext) -> None:
        """Count one failed commit toward the circuit breaker."""
        if not isinstance(event, SessionCompactFailedEvent) or not event.from_extension:
            return
        self.state.note_failure(event.error_message or "the core rejected the compaction commit")

    # ---------------------------------------------------------------- pipeline

    async def _prepare_request(
        self,
        context: ExtensionContext,
        request: ModelRequest,
        view: list[AgentMessage],
        tokens: int,
    ) -> tuple[list[AgentMessage], bool, CompactionCommitRequest | None]:
        """Decide what this request needs: a prepared summary, free layers, or L4."""
        prepared = self.state.prepared
        if prepared is not None:
            applied = await self._apply_prepared(context, view, prepared, tokens)
            if applied is not None:
                return applied

        trigger = self._pending_trigger(tokens)
        if trigger is None:
            self.state.note(
                f"none of the four layers ran: {tokens} token(s) is at or below the gate "
                f"({self.config.budget_threshold})"
            )
            return view, False, None

        if self.config.l1_enabled or self.config.l2_enabled or self.config.l3_enabled:
            free = run_free_layers(
                view,
                results_dir=context.cwd / TOOL_RESULTS_DIRECTORY,
                persist_threshold_chars=self.config.persist_threshold_chars,
                snip_max_messages=self.config.snip_max_messages,
                keep_recent_results=self.config.keep_recent_results,
                placeholder_min_chars=self.config.placeholder_min_chars,
                enabled=(self.config.l1_enabled, self.config.l2_enabled, self.config.l3_enabled),
            )
            for note in free.notes:
                self.state.note(note)
            if free.changed:
                view = list(legalize_view(free.messages))

        changed = view != list(request.messages)
        tokens = estimate_request_tokens(request.system, view)
        self.state.last_request_tokens = tokens

        if trigger == "auto" and not should_compact(tokens, self.config):
            self.state.note(
                f"L4 skipped: the free layers brought the view to {tokens} token(s), "
                f"at or below the gate ({self.config.budget_threshold})"
            )
            return view, changed, None

        view, summarised, commit = await self._run_summary_layer(context, view, tokens, trigger)
        return view, changed or summarised, commit

    def _pending_trigger(self, tokens: int) -> str | None:
        """Return the trigger for this request, consuming the reactive latch.

        The reactive path ignores the gate (an observed provider error is the
        trigger) and stays single-shot per run: taking the trigger marks the
        attempt, which ``before_agent_start`` resets.
        """
        if self.config.reactive_enabled and self.state.reactive_armed:
            if self.state.reactive_attempted:
                return None
            self.state.reactive_attempted = True
            return "reactive"
        if should_compact(tokens, self.config):
            return "auto"
        return None

    async def _run_summary_layer(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        tokens: int,
        trigger: str,
    ) -> tuple[list[AgentMessage], bool, CompactionCommitRequest | None]:
        """L4: one bounded summary, at most one commit, only when it can commit.

        `session_before_compact` runs once every precondition for producing the
        summary holds, and always before the summary model call, so an attempt
        that could never commit asks subscribers for nothing and a cancel saves
        the model call as well as the commit. The free rewrites already applied
        are kept: they are pure view rewrites and never commit anything.

        The gate's decision is also the memory extension's one narrow channel
        into compression: a non-empty provider text is injected into the
        summarizer prompt as material (``summary.render_memory_provider_context``).
        """
        if not self.config.l4_enabled:
            return view, False, None
        if self.state.breaker_tripped():
            self.state.note("L4 skipped: the circuit breaker tripped")
            return view, False, None
        if not self.config.inline_model_attempt:
            self.state.note("L4 deferred: inline attempts are off")
            return view, False, None
        keep_index = self._summary_split(context, view)
        if keep_index is None:
            return view, False, None
        decision = await self._compaction_gate(context, trigger)
        if decision.cancelled:
            return view, False, None
        prepared = await self._prepare_summary(
            context,
            view,
            trigger=trigger,
            count_failures=trigger != "reactive",
            record=False,
            provider_context=decision.context,
        )
        if prepared is None:
            return view, False, None
        keep_index = self._reanchor(view, prepared)
        if keep_index is None:  # cannot normally happen: `_summary_split` just checked it
            self.state.note("L4 skipped: the prepared summary covers no replaceable prefix")
            return view, False, None
        return await self._summarized_view(context, view, prepared, keep_index, tokens)

    async def _apply_prepared(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        prepared: PreparedSummary,
        tokens: int,
    ) -> tuple[list[AgentMessage], bool, CompactionCommitRequest | None] | None:
        """Apply a summary prepared earlier (``/compact`` or the idle phase).

        ``None`` means the request falls through to the gated path: either the
        gate cancelled (the summary stays unspent, and the free rewrites are
        still returned) or the view no longer holds what the summary covers.
        """
        keep_index = self._reanchor(view, prepared)
        if keep_index is None:
            self.state.note("the prepared summary covers no replaceable prefix")
            return None
        decision = await self._compaction_gate(
            context, prepared.trigger, prepared.custom_instructions
        )
        if decision.cancelled:
            return None
        return await self._summarized_view(context, view, prepared, keep_index, tokens)

    async def _summarized_view(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        prepared: PreparedSummary,
        keep_index: int,
        tokens: int,
    ) -> tuple[list[AgentMessage], bool, CompactionCommitRequest | None]:
        """Build the summary view and request its commit."""
        rewritten = [UserMessage(content=summary_message_text(prepared.text)), *view[keep_index:]]
        commit = await self._commit_for(context, prepared, tokens_before=max(1, tokens))
        self.state.consume_prepared()
        self.state.note(
            f"{prepared.layer} kept {len(rewritten) - 1} message(s) verbatim "
            f"({prepared.trigger}, commit={'yes' if commit else 'no'})"
        )
        return rewritten, True, commit

    async def _compaction_gate(
        self,
        context: ExtensionContext,
        trigger: str,
        custom_instructions: str = "",
    ) -> SessionBeforeCompactDecision:
        """Fire `session_before_compact`; return its decision (cancel + context).

        ``custom_instructions`` carries the ``/compact [instructions]`` text of
        the summary being committed, so a handler sees the same instructions the
        summarizer did. ``decision.context`` is the merged non-empty text of the
        subscribing handlers (hermes-agent's ``on_pre_compress``): material for
        the summarizer prompt, not an instruction and not the summary.
        """
        decision = await context.request_session_before_compact(
            reason=_compaction_trigger(trigger),
            custom_instructions=custom_instructions or None,
        )
        if decision.cancelled:
            self.state.note(f"compaction cancelled by the session_before_compact hook ({trigger})")
            self.api.notify(
                "compaction: nothing was compacted for this request: a "
                f"session_before_compact handler cancelled the {trigger} compaction",
                "warning",
            )
        elif decision.context.strip():
            self.state.note(
                "the session_before_compact gate supplied "
                f"{len(decision.context)} character(s) of memory-provider context "
                "for the summary prompt"
            )
        return decision

    def _summary_split(self, context: ExtensionContext, view: list[AgentMessage]) -> int | None:
        """Return the L4 split point, or ``None`` when no attempt can run.

        Both reasons an attempt would be a no-op are decided here, with the same
        notes ``_prepare_summary`` writes, so the caller can decline to fire
        `session_before_compact` for an attempt that could never commit.
        """
        if not context.services.inference.available:
            self.state.note("L4 deferred: this session has no provider for inference")
            return None
        cut = select_cut_index(view, keep_recent_tokens=self.config.keep_recent_tokens)
        if cut is None:
            self.state.note("L4 skipped: the view has no legal user-boundary split")
            return None
        return cut

    async def _prepare_summary(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        *,
        trigger: str,
        count_failures: bool,
        record: bool,
        custom_instructions: str | None = None,
        provider_context: str = "",
        force: bool = False,
    ) -> PreparedSummary | None:
        """Summarize the prefix of ``view``, optionally storing it for later.

        ``custom_instructions`` is the ``/compact [instructions]`` text: it goes
        into the summarizer prompt and onto the prepared summary, so the
        `session_before_compact` gate of the commit that follows carries the same
        text. ``provider_context`` is the material that gate carried; it is fenced
        into the prompt as reference material, never as an instruction.
        ``force`` is ``/compact``'s rule: with no legal cut, summarize the whole
        view and keep nothing (the reference's ``force_compact``).
        """
        inference = context.services.inference
        keep_index = self._summary_split(context, view)
        if keep_index is None and not force:
            return None
        if keep_index is None:
            keep_index = len(view)
            if keep_index < 2:
                return None
        try:
            result = await request_summary(
                inference,
                view[:keep_index],
                config=self.config,
                custom_instructions=custom_instructions,
                provider_context=provider_context,
                previous_summary=self._previous_summary(view),
            )
        except InferenceBusy:
            self.state.note("L4 deferred: a foreground run owns the provider")
            return None
        except InferenceUnavailable as exc:
            self.state.note(f"L4 deferred: {exc}")
            return None
        except SummaryUnavailable as exc:
            if count_failures:
                failures = self.state.note_failure(str(exc))
                self.state.note(f"L4 failed ({failures} consecutive): {exc}")
            else:
                self.state.note(f"L4 reactive attempt failed: {exc}")
            return None
        retained = view[keep_index:]
        summary = PreparedSummary(
            text=result.text,
            trigger=trigger,
            tokens_before=max(1, estimate_message_tokens(view)),
            covered_count=keep_index,
            created_at=time(),
            retained_tail=tuple(message.model_dump(mode="json") for message in retained),
            anchor_key=message_key(retained[0]) if retained else "",
            model=result.model,
            snapshot_id=result.snapshot_id,
            layer="L4",
            custom_instructions=custom_instructions or "",
        )
        if record:
            entry_id = await self._persist_summary(context, summary)
            self.state.record_prepared(summary, entry_id)
        return summary

    def _previous_summary(self, view: list[AgentMessage]) -> str:
        """Return the summary head this view already carries, if any.

        A second compaction over an already-compacted view passes the previous
        summary to the summarizer (the reference's ``previous_summary``), so the
        new summary folds the old one in instead of losing it.
        """
        head = view[0] if view else None
        if isinstance(head, UserMessage):
            return view_summary_text(head.text)
        return ""

    def _reanchor(self, view: list[AgentMessage], prepared: PreparedSummary) -> int | None:
        """Return the first index a prepared summary keeps verbatim.

        The view may have grown since the summary was prepared, so the retained
        tail's first message is looked up by content key and only a miss falls
        back to the recorded cut. ``None`` means there is nothing left to replace.
        """
        if prepared.anchor_key:
            for index, message in enumerate(view):
                if message_key(message) == prepared.anchor_key:
                    return index if index > 0 else None
        index = min(max(prepared.covered_count, 1), len(view))
        if index < 1:
            return None
        return index

    # -------------------------------------------------------------- the commits

    async def _commit_for(
        self,
        context: ExtensionContext,
        summary: PreparedSummary,
        *,
        tokens_before: int,
    ) -> CompactionCommitRequest | None:
        """Build the one ``CompactionCommitRequest`` this request may carry."""
        if self.state.commit_emitted:
            return None
        boundary = await self._boundary_entry_id(context, summary.covered_count)
        if boundary is None:
            self.state.note(
                "no durable commit: the active entry-id snapshot does not cover this boundary"
            )
            return None
        self.state.commit_emitted = True
        return CompactionCommitRequest(
            summary=summary.text,
            first_kept_entry_id=boundary,
            tokens_before=max(1, tokens_before),
            trigger=_compaction_trigger(summary.trigger),
            metadata={
                "layer": summary.layer,
                "coveredCount": summary.covered_count,
                "retainedRows": len(summary.retained_tail),
                "anchorKey": summary.anchor_key,
                "model": summary.model,
                "snapshotId": summary.snapshot_id,
            },
        )

    async def _boundary_entry_id(self, context: ExtensionContext, covered_count: int) -> str | None:
        """Resolve ``first_kept_entry_id`` from the freshest entry-id snapshot."""
        if not self.state.entry_ids:
            await self._refresh_entry_ids(context, context.current_snapshot_id)
        return resolve_first_kept_entry_id(self.state.entry_ids, covered_count)

    async def _refresh_entry_ids(self, context: ExtensionContext, snapshot_id: str | None) -> None:
        """Learn the active durable entry ids from a recorded context snapshot."""
        if not snapshot_id:
            return
        try:
            snapshot = await context.services.snapshots.read(snapshot_id)
        except Exception as exc:  # noqa: BLE001 - diagnostics only, never fatal
            self.state.note(f"entry-id snapshot unavailable: {exc}")
            return
        ids = active_entry_ids(snapshot.payload)
        if ids:
            self.state.record_entry_ids(ids, snapshot_id)

    async def _persist_summary(
        self, context: ExtensionContext, summary: PreparedSummary
    ) -> str | None:
        """Persist a prepared summary as a ``CustomEntry`` (audit trail)."""
        try:
            return await persist_summary(self.api.append_entry, summary)
        except Exception as exc:  # noqa: BLE001 - the commit path still runs
            self.state.note(f"prepared summary was not written durably: {exc}")
            return None

    # ------------------------------------------------------------- the surfaces

    async def compact_command(self, args: str, command_context: ExtensionCommandContext) -> str:
        """``/compact [instructions]``: prepare a manual summary now.

        The optional arguments are the reference's "User instructions for
        summarization": they are appended to the summarizer prompt, carried on
        the prepared summary and handed to the `session_before_compact` gate when
        the next request commits it. ``/compact`` does not wait for the gate: it
        is the user's explicit request, so the cut rule is applied in force mode
        when the view is too short for a regular one.
        """
        context = command_context.api.context
        if not self.rewrite_enabled:
            return f"No summary was prepared: {DISABLED_NOTE}."
        view = list(self.state.last_view) or list(context.transcript)
        if len(view) < 2:
            return "Nothing to summarize yet."
        summary = await self._prepare_summary(
            context,
            view,
            trigger="manual",
            count_failures=True,
            record=True,
            custom_instructions=args.strip() or None,
            force=True,
        )
        if summary is None:
            failures = "; ".join(self.state.notes) or "no reason recorded"
            return f"No summary was prepared ({failures})."
        return COMPACT_RESULT_TEMPLATE.format(rows=summary.covered_count)


def register(extension: LayeredCompaction) -> None:
    """Register the command and the guidance this extension adds."""
    api = extension.api
    api.register_command(
        COMPACT_COMMAND,
        extension.compact_command,
        description="Summarize the older conversation now (L4) and commit it on the next request",
        usage=COMPACT_USAGE,
        aliases=("force-compact",),
    )
    api.add_prompt_guideline(PERSISTED_OUTPUT_GUIDELINE)


def setup(api: ExtensionAPI) -> None:
    """Register this extension's hooks and command on ``api``."""
    extension = LayeredCompaction(api)
    api.on("session_start", cast(ExtensionHandler, extension.on_session_start))
    api.on("before_agent_start", cast(ExtensionHandler, extension.on_before_agent_start))
    api.on("before_provider_request", cast(ExtensionHandler, extension.on_before_provider_request))
    api.on("after_provider_response", cast(ExtensionHandler, extension.on_after_provider_response))
    api.on("message_end", cast(ExtensionHandler, extension.on_message_end))
    api.on("agent_settled", cast(ExtensionHandler, extension.on_agent_settled))
    api.on("session_compact", cast(ExtensionHandler, extension.on_session_compact))
    api.on("session_compact_failed", cast(ExtensionHandler, extension.on_session_compact_failed))
    register(extension)
