"""``setup(api)`` wiring: the L1 → L2 → L3/L4 request pipeline.

Under ``compaction.strategy = "four-layer"`` the core rewrites nothing, so this
extension owns the whole preparation of every Provider request. The pipeline runs
in ``before_provider_request`` only, in this order, and never mutates session
history (the transcript is untouched; only the request view changes):

``L1``
    clear old compactable tool results outside the cacheable prefix.
``L2``
    drop snipped messages, then nudge the model when the view is long.
``L3``
    reuse existing ``MEMORY.md`` / ``USER.md`` content as the summary (no model
    call) when the auto threshold is crossed.
``L4``
    summarize the prefix through ``services.inference.complete`` (bounded by
    ``asyncio.wait_for``), keep a round-aligned recent tail, and ask the core to
    commit the result over ``CompactionCommitRequest``.

At most one commit is requested per request (:meth:`_Extension._commit_for`).

Two host seams shape the port and are documented in the README:

* the hook runs *inside* a foreground run, where the host refuses extension
  inference (``InferenceBusy``). The summary is therefore prepared at
  ``agent_settled`` (idle) or by ``/four-layer-compact``, and an inline attempt is
  made when configured — its ``InferenceBusy`` defers without counting toward the
  circuit breaker.
* the API exposes no entry-id seam, so ``first_kept_entry_id`` is resolved from
  the ``context_entry_ids`` of the last recorded agent snapshot
  (:func:`state.active_entry_ids`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
    InputEvent,
    SessionCompactEvent,
    SessionCompactFailedEvent,
    SessionStartEvent,
)
from run_agent_coding.extensions.api import (
    BeforeProviderRequestResult,
    CompactionCommitRequest,
    CompactionTrigger,
)
from run_agent_coding.host.inference import InferenceBusy, InferenceUnavailable
from run_agent_coding.settings import SettingsError, load_settings
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AgentMessage, AssistantMessage, TextContent, UserMessage
from run_agent_core.provider import ModelRequest
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue

from .auto import (
    SummaryUnavailable,
    auto_compact_threshold,
    reactive_reason_for_error_text,
    reactive_reason_for_status,
    request_summary,
    select_split_index,
    should_auto_compact,
)
from .config import FourLayerConfig, load_config
from .grouping import aligned_start_index
from .memory_compact import (
    SMCompactConfig,
    memory_compaction_diagnostic,
    plan_memory_compaction,
    read_memory_text,
)
from .micro import (
    compactable_summary,
    estimate_message_tokens,
    estimate_request_tokens,
    microcompact,
)
from .prompt import format_compact_summary, get_compact_user_summary_message
from .snip import (
    SNIP_NUDGE_TEXT,
    project_snipped_view,
    select_all_keys,
    select_keys_for_request,
    should_nudge,
    snip_tool_result,
    snip_tool_schema,
    tokens_freed_for,
)
from .state import (
    SNIP_BOUNDARY_TEXT,
    PreparedSummary,
    SessionState,
    SnipBoundary,
    active_entry_ids,
    legalize_view,
    message_key,
    new_boundary,
    persist_boundary,
    persist_summary,
    resolve_first_kept_entry_id,
)

FOUR_LAYER_STRATEGY = "four-layer"
SNIP_TOOL_NAME = "snip"
SNIP_TOOL_DESCRIPTION = (
    "Snip messages from conversation history to free context-window space. "
    "Snipped messages leave the model's view from the next request on; nothing is "
    "deleted from the session file."
)
FORCE_SNIP_USAGE = "/force-snip"
COMPACT_COMMAND = "four-layer-compact"
COMPACT_USAGE = "/four-layer-compact [instructions]"
COMPACT_RESULT_TEMPLATE = (
    "Prepared a manual L4 summary over {rows} message(s); it is committed on the next request."
)


def _compaction_trigger(value: str) -> CompactionTrigger:
    """Narrow a stored trigger string onto the commit channel's literal."""
    if value == "manual":
        return "manual"
    if value == "reactive":
        return "reactive"
    return "auto"


def _anchor_key(view: Sequence[AgentMessage], index: int) -> str:
    """Return the key of the first kept message, or ``""`` when nothing is kept."""
    if index < 0 or index >= len(view):
        return ""
    return message_key(view[index])


def _int_argument(arguments: Mapping[str, JSONValue], name: str) -> int | None:
    value = arguments.get(name)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _string_argument(arguments: Mapping[str, JSONValue], name: str) -> str | None:
    value = arguments.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _string_list_argument(arguments: Mapping[str, JSONValue], name: str) -> tuple[str, ...]:
    value = arguments.get(name)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


class FourLayerCompaction:
    """One loaded extension instance: per-session state plus the hook pipeline."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.state = SessionState()
        self.config = FourLayerConfig()

    # ------------------------------------------------------------------ config

    def configure(self, context: ExtensionContext) -> FourLayerConfig:
        """Load the environment configuration and decide whether to rewrite."""
        try:
            self.config = load_config(context.environment)
        except ValueError as exc:
            self.api.notify(f"claude_compaction: invalid configuration ({exc})", "warning")
            self.config = FourLayerConfig(enabled=False)
        self.state.config = self.config
        self._refresh_strategy(context)
        return self.config

    def _refresh_strategy(self, context: ExtensionContext) -> None:
        """Four-layer rewriting is on only when the session's strategy says so."""
        strategy = "unknown"
        try:
            strategy = load_settings(context.paths, context.cwd).compaction_strategy
        except SettingsError:
            strategy = "invalid"
        self.state.strategy_seen = strategy
        self.state.rewrite_enabled = strategy == FOUR_LAYER_STRATEGY and self.config.enabled

    @property
    def rewrite_enabled(self) -> bool:
        """Return whether this session hands the four layers to this extension."""
        return self.state.rewrite_enabled

    # ------------------------------------------------------------------- hooks

    async def on_session_start(self, event: object, context: ExtensionContext) -> None:
        """Load configuration and reset per-session state."""
        if not isinstance(event, SessionStartEvent):
            return
        self.state.reset_session()
        self.configure(context)

    async def on_before_agent_start(self, event: object, context: ExtensionContext) -> None:
        """Start-of-run reset: the reactive path gets one attempt per run.

        ``event.system_prompt`` is observed but never rewritten: the system prompt is
        part of the cacheable prefix this extension promises not to cut (the rewrite
        always carries ``request.system`` through unchanged).
        """
        if not isinstance(event, BeforeAgentStartEvent):
            return
        self.state.reset_run()

    async def on_input(self, event: object, context: ExtensionContext) -> None:
        """Surface the snip nudge to the user when the view is long enough."""
        if not isinstance(event, InputEvent) or not self.config.user_nudge:
            return
        if not self.rewrite_enabled or not self.config.l2_enabled:
            return
        if self.state.view_message_count < self.config.snip_nudge_threshold:
            return
        if self.state.nudged_count == self.state.view_message_count:
            return
        self.state.nudged_count = self.state.view_message_count
        self.api.notify(SNIP_NUDGE_TEXT, "info")

    async def on_before_provider_request(
        self, event: object, context: ExtensionContext
    ) -> BeforeProviderRequestResult | None:
        """Run L1 → L2 → L3/L4 and return the rewritten view plus one commit."""
        if not isinstance(event, BeforeProviderRequestEvent):
            return None
        if self.state.strategy_seen == "unknown":
            self.configure(context)
        if not self.rewrite_enabled:
            return None

        self.state.begin_request()
        request = event.payload
        view = list(request.messages)
        changed = False

        if self.config.l1_enabled:
            view, changed = self._run_l1(view, changed)
        view = list(legalize_view(view))

        if self.config.l2_enabled:
            view, changed = self._run_l2(view, changed)
        view = list(legalize_view(view))

        if self.config.l2_enabled and should_nudge(view, self.config.snip_nudge_threshold):
            view.append(UserMessage(content=SNIP_NUDGE_TEXT))
            changed = True

        tokens = estimate_request_tokens(request.system, view)
        self.state.last_request_tokens = tokens
        self.state.view_message_count = len(view)
        self.state.last_view = tuple(view)

        view, changed, commit = await self._run_session_layers(context, view, changed, tokens)
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
        if not self.state.last_view:
            return
        trigger = self._pending_trigger(self.state.last_request_tokens)
        if trigger is None:
            return
        await self._prepare_summary(
            context,
            list(self.state.last_view),
            trigger=trigger,
            transcript_path=self._transcript_path(context),
            count_failures=True,
            record=True,
        )

    async def on_session_compact(self, event: object, context: ExtensionContext) -> None:
        """Reconcile after a commit: the durable head is the summary now."""
        if not isinstance(event, SessionCompactEvent) or not event.from_extension:
            return
        summary = self.state.consume_prepared()
        if summary is not None and summary.anchor_key:
            self.state.last_summarized_key = summary.anchor_key
        self.state.note_success()
        self.state.entry_ids = ()
        self.state.entry_ids_snapshot = None

    async def on_session_compact_failed(self, event: object, context: ExtensionContext) -> None:
        """Count one failed commit toward the circuit breaker."""
        if not isinstance(event, SessionCompactFailedEvent) or not event.from_extension:
            return
        self.state.note_failure(event.error_message or "the core rejected the compaction commit")

    # ---------------------------------------------------------------- the layers

    def _run_l1(self, view: list[AgentMessage], changed: bool) -> tuple[list[AgentMessage], bool]:
        """L1: clear old compactable tool results, keeping the recent ones."""
        outcome = microcompact(
            view,
            keep_recent=self.config.keep_recent,
            cached_trigger_threshold=self.config.cached_trigger_threshold,
            now_ms=time() * 1_000,
            time_based_enabled=self.config.time_based_enabled,
            gap_threshold_minutes=self.config.time_gap_threshold_minutes,
        )
        if not outcome.applied:
            return view, changed
        self.state.note(json.dumps(compactable_summary(outcome), sort_keys=True))
        return list(outcome.messages), True

    def _run_l2(self, view: list[AgentMessage], changed: bool) -> tuple[list[AgentMessage], bool]:
        """L2: apply every recorded snip boundary to this request's view."""
        removed = self.state.removed_keys
        if not removed:
            return view, changed
        projected = project_snipped_view(view, removed)
        if projected == view:
            return view, changed
        self.state.note(
            f"L2 projected {len(view) - len(projected)} message(s) out "
            f"({self.state.tokens_freed} tokens freed by boundaries)"
        )
        return projected, True

    async def _run_session_layers(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        changed: bool,
        tokens: int,
    ) -> tuple[list[AgentMessage], bool, CompactionCommitRequest | None]:
        """L3 then L4, at most one commit, and only when something triggered."""
        transcript_path = self._transcript_path(context)
        prepared = self.state.prepared
        trigger = self._pending_trigger(tokens)
        if (
            prepared is None
            and trigger is not None
            and self.config.l3_enabled
            and trigger == "auto"
        ):
            l3 = await self._run_l3(context, view, trigger, transcript_path)
            if l3 is not None:
                view, commit = l3
                return view, True, commit
        if not self.config.l4_enabled:
            return view, changed, None
        if prepared is None:
            if trigger is None:
                return view, changed, None
            if self.state.breaker_tripped():
                self.state.note("L4 skipped: the circuit breaker tripped")
                return view, changed, None
            if not self.config.inline_model_attempt:
                self.state.note("L4 deferred: no prepared summary and inline attempts are off")
                return view, changed, None
            prepared = await self._prepare_summary(
                context,
                view,
                trigger=trigger,
                transcript_path=transcript_path,
                count_failures=trigger != "reactive",
                record=False,
            )
            if prepared is None:
                return view, changed, None
        keep_index = self._apply_prepared(view, prepared)
        if keep_index is None:
            self.state.note("L4 skipped: the prepared summary covers no replaceable prefix")
            return view, changed, None
        summary_head = UserMessage(content=prepared.text)
        rewritten = [summary_head, *view[keep_index:]]
        commit = await self._commit_for(context, prepared, tokens_before=max(1, tokens))
        self.state.consume_prepared()
        self.state.manual_requested = False
        self.state.note(
            f"L4 {prepared.layer} kept {len(rewritten) - 1} message(s) verbatim "
            f"({prepared.trigger}, commit={'yes' if commit else 'no'})"
        )
        return rewritten, True, commit

    async def _run_l3(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        trigger: str,
        transcript_path: str | None,
    ) -> tuple[list[AgentMessage], CompactionCommitRequest | None] | None:
        """L3: reuse existing memory files as the summary, without a model call."""
        memory = read_memory_text(context.paths, context.cwd)
        plan = plan_memory_compaction(
            view,
            memory=memory,
            last_summarized_key=self.state.last_summarized_key,
            transcript_path=transcript_path,
            config=SMCompactConfig(
                min_tokens=self.config.sm_min_tokens,
                min_text_block_messages=self.config.sm_min_text_block_messages,
                max_tokens=self.config.sm_max_tokens,
            ),
            threshold=auto_compact_threshold(self.config),
        )
        if plan is None:
            return None
        summary = PreparedSummary(
            text=plan.summary,
            trigger=trigger,
            tokens_before=max(1, plan.tokens_before),
            replaced_rows=plan.replaced_rows,
            created_at=time(),
            anchor_key=_anchor_key(view, plan.keep_index),
            model="",
            layer="L3",
        )
        entry_id = await self._persist_summary(context, summary)
        self.state.record_prepared(summary, entry_id)
        self.state.note(json.dumps(memory_compaction_diagnostic(plan), sort_keys=True))
        rewritten = [UserMessage(content=summary.text), *view[plan.keep_index :]]
        commit = await self._commit_for(context, summary, tokens_before=summary.tokens_before)
        self.state.consume_prepared()
        return rewritten, commit

    async def _prepare_summary(
        self,
        context: ExtensionContext,
        view: list[AgentMessage],
        *,
        trigger: str,
        transcript_path: str | None,
        count_failures: bool,
        record: bool,
    ) -> PreparedSummary | None:
        """Summarize the prefix of ``view``, optionally storing it for later."""
        inference = context.services.inference
        if not inference.available:
            self.state.note("L4 deferred: this session has no provider for inference")
            return None
        keep_index = select_split_index(view, keep_recent_tokens=self.config.keep_recent_tokens)
        if keep_index <= 0 or keep_index >= len(view):
            self.state.note("L4 skipped: the view has no replaceable prefix")
            return None
        try:
            result = await request_summary(inference, view[:keep_index], config=self.config)
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
        summary = PreparedSummary(
            text=get_compact_user_summary_message(
                format_compact_summary(result.text),
                suppress_follow_up_questions=True,
                transcript_path=transcript_path,
                recent_messages_preserved=True,
            ),
            trigger=trigger,
            tokens_before=max(1, estimate_message_tokens(view)),
            replaced_rows=keep_index,
            created_at=time(),
            anchor_key=message_key(view[keep_index]),
            model=result.model,
            snapshot_id=result.snapshot_id,
            layer="L4",
        )
        if record:
            entry_id = await self._persist_summary(context, summary)
            self.state.record_prepared(summary, entry_id)
        return summary

    def _apply_prepared(self, view: list[AgentMessage], prepared: PreparedSummary) -> int | None:
        """Return the first index the prepared summary keeps verbatim."""
        keep_index = prepared.replaced_rows
        if prepared.anchor_key:
            for index, message in enumerate(view):
                if message_key(message) == prepared.anchor_key:
                    keep_index = index
                    break
        keep_index = aligned_start_index(view, keep_index)
        keep_index = min(max(keep_index, 1), len(view) - 1)
        if keep_index <= 0 or keep_index >= len(view):
            return None
        return keep_index

    # -------------------------------------------------------------- the commits

    def _pending_trigger(self, tokens: int) -> str | None:
        """Return the trigger for this request, consuming the reactive latch.

        The reactive path ignores the threshold switch (an observed provider error
        is the trigger) and stays single-shot per run: taking the trigger marks the
        attempt, which ``before_agent_start`` resets.
        """
        if self.state.manual_requested:
            return "manual"
        if self.config.reactive_enabled and self.state.reactive_armed:
            if self.state.reactive_attempted:
                return None
            self.state.reactive_attempted = True
            return "reactive"
        if should_auto_compact(tokens, self.config):
            return "auto"
        return None

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
        boundary = await self._boundary_entry_id(context, summary.replaced_rows)
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
                "replacedRows": summary.replaced_rows,
                "anchorKey": summary.anchor_key,
                "model": summary.model,
                "snapshotId": summary.snapshot_id,
            },
        )

    async def _boundary_entry_id(self, context: ExtensionContext, replaced_rows: int) -> str | None:
        """Resolve ``first_kept_entry_id`` from the freshest entry-id snapshot."""
        if not self.state.entry_ids:
            await self._refresh_entry_ids(context, context.current_snapshot_id)
        return resolve_first_kept_entry_id(self.state.entry_ids, replaced_rows)

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

    async def _persist_boundary(
        self, context: ExtensionContext, boundary: SnipBoundary
    ) -> str | None:
        """Persist a snip boundary and remember it for this session's projection."""
        entry_id: str | None = None
        try:
            entry_id = await persist_boundary(self.api.append_entry, boundary)
        except Exception as exc:  # noqa: BLE001 - the view rewrite still applies
            self.state.note(f"snip boundary was not written durably: {exc}")
        self.state.record_boundary(boundary, entry_id)
        return entry_id

    async def _persist_summary(
        self, context: ExtensionContext, summary: PreparedSummary
    ) -> str | None:
        """Persist a prepared summary as a ``CustomEntry`` (audit trail)."""
        try:
            return await persist_summary(self.api.append_entry, summary)
        except Exception as exc:  # noqa: BLE001 - the commit path still runs
            self.state.note(f"prepared summary was not written durably: {exc}")
            return None

    def _transcript_path(self, context: ExtensionContext) -> str | None:
        """Return the session's JSONL transcript path when it has an identity."""
        session_id = context.session_id
        if not session_id:
            return None
        return str(context.paths.project_session_dir(context.cwd) / f"{session_id}.jsonl")

    # ------------------------------------------------------------- the surfaces

    async def snip_tool(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """The ``snip`` tool: mark the requested messages as snipped."""
        context = self.api.context
        if not self.rewrite_enabled:
            return AgentToolResult(
                content=[
                    TextContent(
                        text=(
                            "Nothing was snipped: four-layer rewriting is off for this session "
                            f"(compaction.strategy={self.state.strategy_seen!r})."
                        )
                    )
                ]
            )
        messages = list(context.transcript)
        keys = select_keys_for_request(
            messages,
            message_ids=_string_list_argument(arguments, "message_ids"),
            range_start=_int_argument(arguments, "range_start"),
            range_end=_int_argument(arguments, "range_end"),
            keep_recent=_int_argument(arguments, "keep_recent"),
        )
        if not keys:
            return AgentToolResult(
                content=[TextContent(text="No messages matched; no snip boundary was written.")]
            )
        reason = _string_argument(arguments, "reason")
        boundary = new_boundary(
            keys,
            trigger=SNIP_TOOL_NAME,
            reason=reason,
            tokens_freed=tokens_freed_for(messages, frozenset(keys)),
        )
        entry_id = await self._persist_boundary(context, boundary)
        return AgentToolResult(
            content=[TextContent(text=snip_tool_result(len(keys), reason))],
            details={
                "snipped": len(keys),
                "tokensFreed": boundary.tokens_freed,
                "entryId": entry_id,
                "boundary": SNIP_BOUNDARY_TEXT,
            },
        )

    async def force_snip_command(self, args: str, command_context: ExtensionCommandContext) -> str:
        """``/force-snip``: mark every message currently in history as snipped."""
        context = command_context.api.context
        if not self.rewrite_enabled:
            return (
                "Nothing was snipped: four-layer rewriting is off for this session "
                f"(compaction.strategy={self.state.strategy_seen!r})."
            )
        messages = list(context.transcript)
        if not messages:
            return "No messages to snip."
        keys = select_all_keys(messages)
        boundary = new_boundary(
            keys,
            trigger="force-snip",
            tokens_freed=tokens_freed_for(messages, frozenset(keys)),
        )
        entry_id = await self._persist_boundary(context, boundary)
        note = "" if entry_id else " (the boundary is session-local: it was not written durably)"
        return (
            f"Snipped {len(keys)} message(s). Older history will be excluded from the next "
            f"model query.{note}"
        )

    async def compact_command(self, args: str, command_context: ExtensionCommandContext) -> str:
        """``/four-layer-compact``: prepare a manual L4 summary while the session is idle."""
        context = command_context.api.context
        if not self.rewrite_enabled:
            return (
                "No summary was prepared: four-layer rewriting is off for this session "
                f"(compaction.strategy={self.state.strategy_seen!r})."
            )
        view = list(self.state.last_view) or list(context.transcript)
        if len(view) < 2:
            return "Nothing to summarize yet."
        self.state.manual_requested = True
        summary = await self._prepare_summary(
            context,
            view,
            trigger="manual",
            transcript_path=self._transcript_path(context),
            count_failures=True,
            record=True,
        )
        if summary is None:
            self.state.manual_requested = False
            return (
                f"No summary was prepared ({'; '.join(self.state.notes) or 'no reason recorded'})."
            )
        return COMPACT_RESULT_TEMPLATE.format(rows=summary.replaced_rows)


def register(extension: FourLayerCompaction) -> None:
    """Register the tool, the two commands and the guidelines."""
    api = extension.api
    api.register_tool(
        AgentTool(
            name=SNIP_TOOL_NAME,
            label="Snip",
            description=SNIP_TOOL_DESCRIPTION,
            parameters=snip_tool_schema(),
            execute_fn=extension.snip_tool,
            execution_mode="sequential",
        )
    )
    api.register_command(
        "force-snip",
        extension.force_snip_command,
        description="Force snip conversation history at current point",
        usage=FORCE_SNIP_USAGE,
    )
    api.register_command(
        COMPACT_COMMAND,
        extension.compact_command,
        description="Summarize the older conversation now (L4) and commit it on the next request",
        usage=COMPACT_USAGE,
        aliases=("force-compact",),
    )
    api.add_prompt_guideline(
        "When the conversation history grows long, older messages can be snipped with the "
        f"`{SNIP_TOOL_NAME}` tool or the `{FORCE_SNIP_USAGE}` command: they leave the model's "
        "view but stay in the session file."
    )


def setup(api: ExtensionAPI) -> None:
    """Register this extension's hooks, tool and commands on ``api``."""
    extension = FourLayerCompaction(api)
    api.on("session_start", cast(ExtensionHandler, extension.on_session_start))
    api.on("before_agent_start", cast(ExtensionHandler, extension.on_before_agent_start))
    api.on("before_provider_request", cast(ExtensionHandler, extension.on_before_provider_request))
    api.on("after_provider_response", cast(ExtensionHandler, extension.on_after_provider_response))
    api.on("message_end", cast(ExtensionHandler, extension.on_message_end))
    api.on("input", cast(ExtensionHandler, extension.on_input))
    api.on("agent_settled", cast(ExtensionHandler, extension.on_agent_settled))
    api.on("session_compact", cast(ExtensionHandler, extension.on_session_compact))
    api.on("session_compact_failed", cast(ExtensionHandler, extension.on_session_compact_failed))
    register(extension)
