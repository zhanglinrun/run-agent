"""Hook-pipeline and end-to-end tests for the cheap-first compaction extension.

Two levels, both offline:

* the extension is driven directly through its registered handlers with stub host
  objects (the shape ``run_agent_extensions.hermes_memory``'s tests use), which is
  where the token gate, the free-layer batch, the one-commit-per-request rule, the
  breaker and the reactive latch are pinned;
* the end-to-end tests load the real package into a real ``CodingApplication`` with
  a fake provider and assert what the provider actually received (compacted view,
  persisted results, one durable ``CompactionEntry``), while the session JSONL
  history stays untouched.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from tests.redesign.test_coding_application import options

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import (
    AfterProviderResponseEvent,
    BeforeAgentStartEvent,
    BeforeProviderRequestEvent,
    ExtensionCommandContext,
    SessionBeforeCompactDecision,
    SessionCompactEvent,
    SessionCompactFailedEvent,
    SessionStartEvent,
)
from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceRequest,
    InferenceResult,
)
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider import ModelRequest
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry, CustomEntry
from run_agent_core.tools import AgentTool
from run_agent_extensions.layered_compaction import (
    MEMORY_PROVIDER_CONTEXT_OPEN,
    PERSISTED_OUTPUT_MARKER,
    SUMMARIZATION_SYSTEM_PROMPT,
    LayeredCompaction,
    PreparedSummary,
    SummaryUnavailable,
    setup,
)

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "run_agent_extensions" / "layered_compaction"
)
NOW_MS = 1_700_000_000_000

EXPECTED_HOOKS = {
    "session_start",
    "before_agent_start",
    "before_provider_request",
    "after_provider_response",
    "message_end",
    "agent_settled",
    "session_compact",
    "session_compact_failed",
}

# The gate sits at 80% of the budget: 1000 tokens -> 800, and the retained-tail
# budget follows as 250 tokens. Small enough that a test view crosses it.
LOW_GATE_ENV = {"COMPACTION_LAYER_CONTEXT_WINDOW": "1000"}
GATE_TOKENS = 800


def user(text: str) -> UserMessage:
    return UserMessage(content=text, timestamp=NOW_MS)


def assistant(text: str, *calls: ToolCall, response_id: str | None = None) -> AssistantMessage:
    blocks: list[object] = [TextContent(text=text)] if text else []
    blocks.extend(calls)
    return AssistantMessage(
        content=blocks,
        model="test",
        provider="test",
        stop_reason="toolUse" if calls else "stop",
        response_id=response_id,
        timestamp=NOW_MS,
    )


def result(call_id: str, text: str) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id, tool_name="read", content=[TextContent(text=text)], timestamp=NOW_MS
    )


def view_with_tools(rounds: int, *, size: int = 400, text: str = "answer") -> list[object]:
    messages: list[object] = [user("first question")]
    for index in range(rounds):
        call_id = f"call-{index}"
        messages.append(assistant(text, ToolCall(id=call_id, name="read", arguments={})))
        messages.append(result(call_id, "x" * size))
    return messages


def big_view() -> list[object]:
    """A view over the test gate whose tail starts at a legal user boundary."""
    messages: list[object] = [user("first question"), user("older turn")]
    for index in range(3):
        messages.append(
            assistant("answer" * 20, ToolCall(id=f"call-{index}", name="read", arguments={}))
        )
        messages.append(result(f"call-{index}", "x" * 4_000))
    return messages


def huge_result_view() -> list[object]:
    """A view over the gate whose only oversized result L1 can persist."""
    messages: list[object] = [user("first question")]
    for index in range(3):
        messages.append(
            assistant("answer" * 20, ToolCall(id=f"call-{index}", name="read", arguments={}))
        )
        messages.append(result(f"call-{index}", "x" * 4_000))
    messages.append(assistant("", ToolCall(id="call-huge", name="read", arguments={})))
    messages.append(result("call-huge", "y" * 30_000))
    return messages


def small_view() -> list[object]:
    return view_with_tools(2, size=40)


def request_for(messages: Sequence[object], *, system: str = "SYS") -> ModelRequest:
    return ModelRequest(
        model="test", system=system, messages=tuple(messages), tools=(), session_id=None
    )


class StubUi:
    """UI bridge stub: notifications are recorded, dialogs answer no-op defaults."""

    def __init__(self) -> None:
        self.has_ui = False
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append((message, level))

    def set_status(self, source: str, key: str, text: str | None) -> None:
        return None

    def clear_status(self, source: str | None = None) -> None:
        return None

    async def select(
        self, title: str, options: object, *, timeout: float | None = None
    ) -> str | None:
        return None

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        return False

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        return None


class StubInference:
    """Scripted ``InferenceService``: text, errors, or a busy refusal."""

    def __init__(
        self,
        *,
        text: str = "<summary>## Goal\nship it</summary>",
        errors: Sequence[Exception] = (),
        busy: bool = False,
        available: bool = True,
    ) -> None:
        self.text = text
        self.errors = list(errors)
        self.busy = busy
        self._available = available
        self.calls = 0
        self.requests: list[InferenceRequest] = []

    @property
    def available(self) -> bool:
        return self._available

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.calls += 1
        self.requests.append(request)
        if self.busy:
            raise InferenceBusy("a foreground run is in flight")
        if self.errors:
            raise self.errors.pop(0)
        return InferenceResult(
            text=self.text, model="test", snapshot_id="snap-1", input_tokens=11, output_tokens=7
        )


class StubSnapshots:
    """The entry-id seam: a context-snapshot payload with ``context_entry_ids``."""

    def __init__(self, entry_ids: Sequence[str]) -> None:
        self.entry_ids = tuple(entry_ids)
        self.reads: list[str] = []

    async def read(self, snapshot_id: str) -> object:
        self.reads.append(snapshot_id)
        return _Snapshot(payload={"context_entry_ids": list(self.entry_ids)})


class _Snapshot:
    def __init__(self, *, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)


class StubServices:
    def __init__(self, *, inference: StubInference, snapshots: StubSnapshots) -> None:
        self.inference = inference
        self.snapshots = snapshots


class StubContext:
    """The slice of ``ExtensionContext`` this extension reads."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        inference: StubInference | None = None,
        snapshots: StubSnapshots | None = None,
        session_id: str = "session-1",
        snapshot_id: str | None = "snap-current",
        transcript: Sequence[object] = (),
        ui: StubUi | None = None,
        context_window_tokens: int = 128_000,
    ) -> None:
        self.paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")
        self.cwd = tmp_path
        self.environment = dict(environment or {})
        self.session_id = session_id
        self.current_snapshot_id = snapshot_id
        self.transcript: tuple[object, ...] = tuple(transcript)
        self.ui = ui or StubUi()
        self.has_ui = self.ui.has_ui
        self.context_window_tokens = context_window_tokens
        self.services = StubServices(
            inference=inference or StubInference(),
            snapshots=snapshots or StubSnapshots(("e0", "e1", "e2", "e3")),
        )
        # The host seam the extension calls before it commits.
        self.compaction_requests: list[tuple[str, str | None]] = []
        self.compaction_cancelled = False
        self.compaction_context = ""

    async def request_session_before_compact(
        self, *, reason: str, custom_instructions: str | None = None
    ) -> SessionBeforeCompactDecision:
        """Record the gate the extension fires; the decision is scripted by the test."""
        self.compaction_requests.append((reason, custom_instructions))
        return SessionBeforeCompactDecision(
            cancelled=self.compaction_cancelled, context=self.compaction_context
        )


class StubApi:
    """Registers everything ``setup(api)`` hands over, for direct invocation."""

    def __init__(self, context: StubContext) -> None:
        self.context = context
        self.tools: dict[str, AgentTool] = {}
        self.commands: dict[str, object] = {}
        self.handlers: dict[str, list[object]] = {}
        self.guidelines: list[str] = []
        self.entries: list[tuple[str, str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, str]] = []

    def register_tool(self, tool: AgentTool) -> None:
        self.tools[tool.name] = tool

    def register_command(
        self,
        name: str,
        handler: object,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self.commands[name] = handler

    def on(self, event: str, handler: object | None = None) -> object:
        if handler is None:
            return lambda decorated: decorated
        self.handlers.setdefault(event, []).append(handler)
        return handler

    def add_prompt_guideline(self, guideline: str) -> None:
        self.guidelines.append(guideline)

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append((message, level))

    async def append_entry(self, namespace: str, data: dict[str, Any]) -> str:
        entry_id = f"entry-{len(self.entries)}"
        self.entries.append((entry_id, namespace, data))
        return entry_id


async def started(
    tmp_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    inference: StubInference | None = None,
    snapshots: StubSnapshots | None = None,
    transcript: Sequence[object] = (),
    snapshot_id: str | None = "snap-current",
    context_window: int = 128_000,
) -> tuple[StubApi, LayeredCompaction, StubContext]:
    context = StubContext(
        tmp_path,
        environment=environment,
        inference=inference,
        snapshots=snapshots,
        transcript=transcript,
        snapshot_id=snapshot_id,
        context_window_tokens=context_window,
    )
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]
    await fire(api, "session_start", SessionStartEvent(reason="startup"), context)
    return api, extension_of(api), context


def extension_of(api: StubApi) -> LayeredCompaction:
    """The instance ``setup`` bound its handlers to."""
    handler = api.handlers["before_provider_request"][0]
    assert isinstance(handler.__self__, LayeredCompaction)  # type: ignore[attr-defined]
    return handler.__self__  # type: ignore[attr-defined,no-any-return]


async def fire(api: StubApi, event: str, payload: object, context: StubContext) -> object:
    result: object = None
    for handler in api.handlers[event]:
        result = await handler(payload, context)  # type: ignore[operator]
    return result


async def request_through(
    api: StubApi, context: StubContext, messages: Sequence[object], *, system: str = "SYS"
) -> object:
    event = BeforeProviderRequestEvent(payload=request_for(messages, system=system))
    return await fire(api, "before_provider_request", event, context)


async def command(api: StubApi, name: str, args: str = "") -> str | None:
    handler = api.commands[name]
    return await handler(args, ExtensionCommandContext(name=name, args=args, api=api))  # type: ignore[operator]


def results_dir(tmp_path: Path) -> Path:
    return tmp_path / ".run" / "tool-results"


# --------------------------------------------------------------- registration


def test_setup_registers_the_hook_and_command_surface(tmp_path: Path) -> None:
    context = StubContext(tmp_path)
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]

    assert set(api.handlers) == EXPECTED_HOOKS
    assert api.tools == {}, "the snip tool is gone: the pipeline needs no model cooperation"
    assert set(api.commands) == {"compact"}
    assert api.guidelines and ".run/tool-results" in api.guidelines[0]


# -------------------------------------------------------------------- the gate


async def test_below_the_gate_not_a_single_layer_runs(tmp_path: Path) -> None:
    """Under 80% of the budget the request is returned untouched, disk included."""
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)

    assert await request_through(api, context, small_view()) is None

    assert extension.state.last_request_tokens <= GATE_TOKENS
    assert not (tmp_path / ".run").exists(), "no layer touched the disk"
    assert api.entries == []
    assert any("at or below the gate" in note for note in extension.state.notes)


async def test_the_gate_is_the_budget_and_the_free_layers_run_only_above_it(
    tmp_path: Path,
) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)
    view = huge_result_view()

    outcome = await request_through(api, context, view)

    assert outcome is not None
    rewritten = list(outcome.request.messages)
    previews = [message for message in rewritten if message.text.startswith(PERSISTED_OUTPUT_MARKER)]
    assert len(previews) == 1, "L1 ran on the view that crossed the gate"
    written = results_dir(tmp_path) / "call-huge.txt"
    assert written.read_text(encoding="utf-8") == "y" * 30_000
    assert str(written) in previews[0].text
    assert outcome.compaction is None, "the free layers brought the view back under the gate"
    assert any("L1 persisted 1" in note for note in extension.state.notes)
    assert any("L4 skipped" in note for note in extension.state.notes)


async def test_the_paid_layer_runs_only_when_the_free_layers_are_not_enough(
    tmp_path: Path,
) -> None:
    inference = StubInference()
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    outcome = await request_through(api, context, big_view())

    assert outcome is not None and outcome.compaction is not None
    assert inference.calls == 1
    assert list(outcome.request.messages)[0].text.startswith("Previous conversation summary:")


async def test_a_disabled_extension_leaves_the_request_alone(tmp_path: Path) -> None:
    api, extension, context = await started(
        tmp_path, environment={"COMPACTION_LAYER_ENABLED": "0", **LOW_GATE_ENV}
    )

    assert await request_through(api, context, big_view()) is None
    assert extension.rewrite_enabled is False
    assert "compaction extension is off" in (await command(api, "compact") or "")
    assert not (tmp_path / ".run").exists()


async def test_the_budget_is_the_model_window_or_a_tighter_override(tmp_path: Path) -> None:
    """The extension computes no window of its own: it reads the bound session's."""
    _api, wide, _context = await started(
        tmp_path,
        environment={"COMPACTION_LAYER_CONTEXT_WINDOW": "60000"},
        context_window=200_000,
    )
    assert wide.config.context_window == 60_000, "an explicit override tightens it"
    assert wide.config.keep_recent_tokens == 15_000, "the tail budget follows the window"

    _api, narrow, _context = await started(
        tmp_path,
        environment={"COMPACTION_LAYER_CONTEXT_WINDOW": "60000"},
        context_window=30_000,
    )
    assert narrow.config.context_window == 30_000, "the model window is the ceiling"

    api, unset, context = await started(tmp_path, context_window=50_000)
    assert unset.config.context_window == 50_000, "unset means the model window"
    assert unset.config.keep_recent_tokens == 12_500

    # A model switch between runs is picked up from the same bound value.
    context.context_window_tokens = 40_000
    await fire(
        api, "before_agent_start", BeforeAgentStartEvent(prompt="p", system_prompt="S"), context
    )
    assert unset.config.context_window == 40_000
    assert unset.config.keep_recent_tokens == 10_000


# ------------------------------------------------------------------- commits


async def test_a_prepared_summary_produces_exactly_one_commit(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)
    summary = PreparedSummary(
        text="## Goal\nship it",
        trigger="auto",
        tokens_before=123,
        covered_count=1,
        created_at=1.0,
    )
    extension.state.record_prepared(summary, None)

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None
    assert commit.trigger == "auto"
    assert commit.summary == summary.text
    assert commit.tokens_before > 0
    assert commit.first_kept_entry_id == "e1", "the entry id comes from the snapshot payload"
    assert commit.metadata is not None and commit.metadata["layer"] == "L4"
    head = list(outcome.request.messages)[0]
    assert head.text == "Previous conversation summary:\n## Goal\nship it"
    assert context.services.snapshots.reads == ["snap-current"]

    # One commit per request: the latch refuses a second request in the same hook run.
    second = await extension._commit_for(context, summary, tokens_before=5)
    assert second is None


async def test_a_prepared_summary_is_applied_below_the_gate(tmp_path: Path) -> None:
    """``/compact`` is the user's decision: it does not wait for the gate."""
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)
    extension.state.record_prepared(
        PreparedSummary(
            text="## Goal\nmanual",
            trigger="manual",
            tokens_before=9,
            covered_count=1,
            created_at=1.0,
        ),
        None,
    )

    outcome = await request_through(api, context, small_view())

    assert outcome is not None and outcome.compaction is not None
    assert list(outcome.request.messages)[0].text.endswith("## Goal\nmanual")
    assert context.compaction_requests == [("manual", None)]


async def test_a_commit_is_skipped_with_a_diagnostic_without_an_entry_id_snapshot(
    tmp_path: Path,
) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV, snapshot_id=None)
    extension.state.record_prepared(
        PreparedSummary(
            text="## Goal\nx", trigger="auto", tokens_before=10, covered_count=1, created_at=1.0
        ),
        None,
    )

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None
    assert outcome.compaction is None
    assert any("no durable commit" in note for note in extension.state.notes)


async def test_the_commit_gate_fires_with_the_trigger_the_commit_will_carry(
    tmp_path: Path,
) -> None:
    """A commit attempt asks `session_before_compact` first, with its own trigger."""
    inference = StubInference()
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    outcome = await request_through(api, context, big_view())

    assert context.compaction_requests == [("auto", None)], (
        "the gate carries the extension's own trigger, not a mapped core reason"
    )
    assert inference.requests[-1] is not None
    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None and commit.trigger == "auto", (
        "the hook reason and the commit trigger are the same string"
    )
    assert not any("cancelled" in note for note in _extension.state.notes)


async def test_a_cancelled_commit_gate_skips_l4_and_reports_a_warning(tmp_path: Path) -> None:
    """A cancel drops the commit, but keeps the free rewrites of the request."""
    api, extension, context = await started(
        tmp_path, environment={**LOW_GATE_ENV, "COMPACTION_LAYER_KEEP_RECENT_RESULTS": "0"}
    )
    extension.state.record_prepared(
        PreparedSummary(
            text="## Goal\nmust not commit",
            trigger="auto",
            tokens_before=10,
            covered_count=1,
            created_at=1.0,
        ),
        None,
    )
    context.compaction_cancelled = True

    outcome = await request_through(api, context, huge_result_view())

    assert context.compaction_requests == [("auto", None)]
    assert extension.state.prepared is not None, "the cancel kept the summary unspent"
    assert outcome is not None, "the free rewrites are still returned"
    assert outcome.compaction is None, "a cancelled compaction never commits"
    texts = [message.text for message in outcome.request.messages]
    assert any(text.startswith("[Earlier tool result compacted]") for text in texts)
    assert any(text.startswith(PERSISTED_OUTPUT_MARKER) for text in texts)
    assert "must not commit" not in "".join(texts)
    assert any("cancelled" in note for note in extension.state.notes)
    assert api.notifications == [
        (
            "compaction: nothing was compacted for this request: a "
            "session_before_compact handler cancelled the auto compaction",
            "warning",
        )
    ]


async def test_an_attempt_that_cannot_run_does_not_fire_the_gate(tmp_path: Path) -> None:
    """No provider means no commit attempt, so subscribers are not asked."""
    inference = StubInference(available=False)
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    outcome = await request_through(api, context, big_view())

    assert outcome is None
    assert inference.calls == 0
    assert context.compaction_requests == [], "an attempt that cannot commit fires no gate"
    assert any("no provider" in note for note in extension.state.notes)


async def test_a_view_without_a_replaceable_prefix_does_not_fire_the_gate(tmp_path: Path) -> None:
    """A view the summarizer cannot split is not a commit attempt either."""
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)
    view = [user("x" * 40_000)]

    outcome = await request_through(api, context, view)

    assert outcome is None
    assert context.compaction_requests == []
    assert any("no legal user-boundary split" in note for note in extension.state.notes)


async def test_a_cancelled_gate_skips_the_summary_model_call(tmp_path: Path) -> None:
    """The gate fires before the summarizer runs, so a cancel saves that call."""
    inference = StubInference()
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    context.compaction_cancelled = True

    outcome = await request_through(api, context, big_view())

    assert context.compaction_requests == [("auto", None)]
    assert inference.calls == 0, "the cancel landed before the summarizer ran"
    assert outcome is None


async def test_l4_defers_on_a_foreground_run_without_tripping_the_breaker(tmp_path: Path) -> None:
    inference = StubInference(busy=True)
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    outcome = await request_through(api, context, big_view())
    await request_through(api, context, big_view())
    await request_through(api, context, big_view())

    assert inference.calls == 3, "each request tried once"
    assert not extension.state.breaker_tripped(), "a busy host is a deferral, not a failure"
    assert outcome is None, "a deferral leaves the request untouched"
    assert any("foreground run" in note for note in extension.state.notes)


async def test_l4_breaker_stops_after_three_consecutive_failures(tmp_path: Path) -> None:
    inference = StubInference(errors=[RuntimeError("provider exploded")] * 3)
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    for _ in range(3):
        await request_through(api, context, big_view())

    assert inference.calls == 3
    assert extension.state.breaker_tripped()
    assert extension.state.consecutive_failures == 3

    outcome = await request_through(api, context, big_view())

    assert inference.calls == 3, "the breaker stops further attempts"
    assert outcome is None
    assert any("circuit breaker" in note for note in extension.state.notes)


async def test_a_prepared_summary_is_used_when_inference_is_unavailable(tmp_path: Path) -> None:
    inference = StubInference(available=False)
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    extension.state.record_prepared(
        PreparedSummary(
            text="## Goal\ncached",
            trigger="auto",
            tokens_before=9,
            covered_count=1,
            created_at=1.0,
        ),
        None,
    )

    outcome = await request_through(api, context, view_with_tools(2))

    assert inference.calls == 0
    assert outcome is not None and outcome.compaction is not None
    assert list(outcome.request.messages)[0].text.endswith("## Goal\ncached")


# ------------------------------------------------------------------ reactive


async def test_reactive_attempts_once_per_run_regardless_of_the_gate(tmp_path: Path) -> None:
    inference = StubInference(text="<summary>emergency</summary>")
    api, extension, context = await started(
        tmp_path,
        environment={"COMPACTION_LAYER_KEEP_RECENT_TOKENS": "250"},
        inference=inference,
    )
    view = big_view()

    # The default 128k budget means the proactive gate is far away.
    await fire(
        api,
        "message_end",
        MessageEndEvent(
            message=assistant("", response_id=None).model_copy(
                update={
                    "stop_reason": "error",
                    "error_message": "prompt is too long: 200001 tokens",
                }
            )
        ),
        context,
    )
    outcome = await request_through(api, context, view)

    assert inference.calls == 1
    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None and commit.trigger == "reactive"
    assert extension.state.reactive_attempted is True
    assert context.compaction_requests == [("reactive", None)], (
        "the reactive path also asks the session_before_compact hook first"
    )

    # Still the same run: the latch blocks a second reactive attempt.
    assert await request_through(api, context, view) is None
    assert inference.calls == 1

    # A new run re-arms it.
    await fire(
        api, "before_agent_start", BeforeAgentStartEvent(prompt="p", system_prompt="S"), context
    )
    assert extension.state.reactive_attempted is False
    assert await request_through(api, context, view) is not None
    assert inference.calls == 2


async def test_a_media_size_status_arms_reactive_and_a_plain_error_does_not(
    tmp_path: Path,
) -> None:
    inference = StubInference()
    api, extension, context = await started(
        tmp_path,
        environment={"COMPACTION_LAYER_KEEP_RECENT_TOKENS": "250"},
        inference=inference,
    )

    await fire(
        api, "after_provider_response", AfterProviderResponseEvent(status=400, headers={}), context
    )
    assert extension.state.reactive_armed is False, "a 400 alone proves nothing"
    assert await request_through(api, context, small_view()) is None
    assert inference.calls == 0

    await fire(
        api, "after_provider_response", AfterProviderResponseEvent(status=413, headers={}), context
    )
    assert extension.state.reactive_armed is True
    await fire(
        api,
        "message_end",
        MessageEndEvent(
            message=assistant("", response_id=None).model_copy(
                update={"stop_reason": "error", "error_message": "rate limited"}
            )
        ),
        context,
    )
    assert await request_through(api, context, big_view()) is not None
    assert inference.calls == 1


# ------------------------------------------------------------------------ L4


async def test_the_gate_context_is_injected_into_the_summarizer_prompt(tmp_path: Path) -> None:
    """A handler's text reaches the summary prompt as fenced material."""
    inference = StubInference()
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    context.compaction_context = "durable: the user runs pytest with -q"

    outcome = await request_through(api, context, big_view())

    assert outcome is not None and outcome.compaction is not None
    prompt = inference.requests[-1].prompt
    assert MEMORY_PROVIDER_CONTEXT_OPEN in prompt
    assert "durable: the user runs pytest with -q" in prompt
    assert "reference material only" in prompt, "the fence says how to read the text"
    assert inference.requests[-1].system == SUMMARIZATION_SYSTEM_PROMPT
    assert inference.requests[-1].tool_names == ()
    assert any("memory-provider context" in note for note in extension.state.notes), (
        "the contribution is reported in the request's diagnostics"
    )


async def test_no_fence_is_rendered_when_no_handler_contributes(tmp_path: Path) -> None:
    """An empty contribution leaves the prompt byte-identical to the plain one."""
    inference = StubInference()
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )

    outcome = await request_through(api, context, big_view())

    assert outcome is not None and outcome.compaction is not None
    assert context.compaction_requests == [("auto", None)]
    assert MEMORY_PROVIDER_CONTEXT_OPEN not in inference.requests[-1].prompt


async def test_a_failed_summary_leaves_the_view_and_the_commit_alone(tmp_path: Path) -> None:
    """A broken attempt is not a summary: no view rewrite, no commit."""
    inference = StubInference(errors=[SummaryUnavailable("the summary request returned no text")])
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    view = big_view()

    outcome = await request_through(api, context, view)

    assert outcome is None, "no rewrite is returned and nothing is committed"
    assert context.compaction_requests == [("auto", None)]
    assert extension.state.prepared is None
    assert any("L4 failed" in note for note in extension.state.notes)


async def test_the_second_summary_folds_in_the_previous_one(tmp_path: Path) -> None:
    """A view that already carries a summary head passes it to the summarizer."""
    inference = StubInference()
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    view = big_view()
    view[0] = UserMessage(content="Previous conversation summary:\nthe older story")

    await request_through(api, context, view)

    prompt = inference.requests[-1].prompt
    assert "Previous summary:\nthe older story" in prompt
    assert "(none)" not in prompt


async def test_memory_files_are_not_a_summary_any_more(tmp_path: Path) -> None:
    """Regression: memory is never a summary, so a MEMORY.md compacts nothing."""
    inference = StubInference(text="<summary>model work</summary>")
    api, _extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    (context.paths.home / "MEMORY.md").parent.mkdir(parents=True, exist_ok=True)
    (context.paths.home / "MEMORY.md").write_text("Runs pytest with -q", encoding="utf-8")
    (context.paths.home / "USER.md").write_text("Prefers short answers", encoding="utf-8")

    outcome = await request_through(api, context, big_view())

    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None
    assert inference.calls == 1, "a memory file never replaces the summary model call"
    assert "model work" in commit.summary, "the summary is the model's answer"
    assert "Runs pytest with -q" not in commit.summary
    assert "Prefers short answers" not in commit.summary
    assert "Runs pytest with -q" not in inference.requests[-1].prompt, (
        "the memory files are not read into the summarizer prompt either"
    )
    assert commit.metadata is not None and commit.metadata["layer"] == "L4"


# ------------------------------------------------------------------ surfaces


async def test_the_manual_command_prepares_a_summary_the_next_request_commits(
    tmp_path: Path,
) -> None:
    inference = StubInference(text="<summary>manual work</summary>")
    api, extension, context = await started(tmp_path, inference=inference)
    context.transcript = tuple(view_with_tools(2))
    await request_through(api, context, view_with_tools(2))

    message = await command(api, "compact")

    assert message is not None and message.startswith("Prepared a manual summary over")
    assert inference.calls == 1
    assert extension.state.prepared is not None
    assert api.entries[0][1] == "layered_compaction.summary"

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None and commit.trigger == "manual"
    assert commit.summary == "manual work"


async def test_the_manual_command_threads_its_instructions_into_prompt_and_gate(
    tmp_path: Path,
) -> None:
    """``/compact <instructions>`` reaches the prompt and the commit gate."""
    inference = StubInference(text="<summary>manual work</summary>")
    api, extension, context = await started(tmp_path, inference=inference)
    context.transcript = tuple(view_with_tools(2))
    await request_through(api, context, view_with_tools(2))
    inference.requests.clear()

    message = await command(api, "compact", "只改测试")

    assert message is not None and message.startswith("Prepared a manual summary over")
    prompt = inference.requests[-1].prompt
    assert "User instructions for summarization:\n只改测试" in prompt, (
        "the command arguments are appended to the summarizer prompt"
    )
    assert extension.state.prepared is not None
    assert extension.state.prepared.custom_instructions == "只改测试", (
        "the prepared summary carries the instructions for its commit"
    )
    assert context.compaction_requests == [], "preparing does not gate"

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None and outcome.compaction is not None
    assert context.compaction_requests == [("manual", "只改测试")], (
        "the gate that guards the commit sees the same instructions"
    )


async def test_the_manual_command_forces_a_summary_for_a_short_view(tmp_path: Path) -> None:
    """With no legal cut, ``/compact`` summarizes the whole view (force mode)."""
    inference = StubInference()
    api, extension, context = await started(tmp_path, inference=inference)
    context.transcript = tuple([user("a short question"), assistant("a short answer")])
    await request_through(api, context, list(context.transcript))

    message = await command(api, "compact")

    assert message is not None and message.startswith("Prepared a manual summary over")
    assert extension.state.prepared is not None
    assert extension.state.prepared.covered_count == 2, "everything is covered, no tail is kept"
    assert extension.state.prepared.retained_tail == ()


async def test_the_manual_command_reports_why_nothing_was_prepared(tmp_path: Path) -> None:
    inference = StubInference(busy=True)
    api, _extension, context = await started(tmp_path, inference=inference)
    context.transcript = tuple(view_with_tools(2))

    message = await command(api, "compact")

    assert message is not None and message.startswith("No summary was prepared (")
    assert "deferred" in message


async def test_agent_settled_prepares_a_summary_while_the_session_is_idle(
    tmp_path: Path,
) -> None:
    inference = StubInference(text="<summary>idle summary</summary>")
    environment = {**LOW_GATE_ENV, "COMPACTION_LAYER_INLINE_MODEL_ATTEMPT": "0"}
    api, extension, context = await started(tmp_path, environment=environment, inference=inference)
    assert await request_through(api, context, big_view()) is None, (
        "with inline attempts off the request is sent as the free layers left it"
    )
    assert inference.calls == 0

    await fire(
        api,
        "agent_settled",
        AgentSettledEvent(
            run_id="r1",
            session_id="session-1",
            branch_id="b1",
            status="succeeded",
            head_id="h1",
            watermark=1,
            snapshot_id="snap-current",
        ),
        context,
    )

    assert inference.calls == 1, "the idle phase is where the model call happens"
    assert extension.state.prepared is not None
    assert context.services.snapshots.reads == ["snap-current"]

    # The prepared summary is committed on the next request.
    outcome = await request_through(api, context, big_view())

    assert outcome is not None and outcome.compaction is not None
    assert outcome.compaction.trigger == "auto"


async def test_the_idle_phase_stays_silent_below_the_gate(tmp_path: Path) -> None:
    inference = StubInference()
    api, extension, context = await started(
        tmp_path, environment=LOW_GATE_ENV, inference=inference
    )
    await request_through(api, context, small_view())

    await fire(
        api,
        "agent_settled",
        AgentSettledEvent(
            run_id="r1",
            session_id="session-1",
            branch_id="b1",
            status="succeeded",
            head_id="h1",
            watermark=1,
            snapshot_id="snap-current",
        ),
        context,
    )

    assert inference.calls == 0
    assert extension.state.prepared is None


async def test_session_compact_events_reconcile_the_state(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_GATE_ENV)
    extension.state.record_prepared(
        PreparedSummary(
            text="## Goal\ncommitted",
            trigger="auto",
            tokens_before=5,
            covered_count=1,
            created_at=1.0,
        ),
        None,
    )
    extension.state.entry_ids = ("stale",)

    await fire(
        api,
        "session_compact",
        SessionCompactEvent(reason="threshold", from_extension=True),
        context,
    )

    assert extension.state.prepared is None
    assert extension.state.breaker_reason is None
    assert extension.state.entry_ids == ()
    assert extension.state.consecutive_failures == 0

    await fire(
        api,
        "session_compact_failed",
        SessionCompactFailedEvent(
            reason="threshold", from_extension=True, error_message="CAS conflict"
        ),
        context,
    )
    assert extension.state.consecutive_failures == 1


# --------------------------------------------------------------- end to end


class ToolThenReplyProvider:
    """Fake provider: reads each file once, then answers, recording every request.

    The tool-list gate keeps the summarizer call (which passes no tools) from
    consuming a read, and the counter keeps the fake finite whatever the extension
    does to the view.
    """

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = paths
        self.reads = 0
        self.requests: list[dict[str, object]] = []

    async def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[object],
        tools: Sequence[object],
        **_: object,
    ) -> AsyncIterator[object]:
        self.requests.append(
            {
                "system": system,
                "messages": [message.model_dump(mode="json") for message in messages],
                "tool_names": [getattr(tool, "name", "") for tool in tools],
            }
        )
        tool_names = [getattr(tool, "name", "") for tool in tools]
        if "read" in tool_names and self.reads < len(self.paths):
            call_id = f"read-{self.reads}"
            target = self.paths[self.reads]
            self.reads += 1
            yield AssistantDoneEvent(
                reason="toolUse",
                message=AssistantMessage(
                    content=[ToolCall(id=call_id, name="read", arguments={"path": target})],
                    model=model,
                    provider="test",
                    stop_reason="toolUse",
                ),
            )
            return
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="done reading")],
                model=model,
                provider="test",
                stop_reason="stop",
            ),
        )


def session_settings(context_window: int) -> ProviderSettings:
    return ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("test",),
                default_model="test",
                api_key_env="LAYERED_COMPACTION_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def installed_extension() -> Path:
    assert (PACKAGE_DIR / "extension.py").is_file()
    return PACKAGE_DIR


def e2e_options(tmp_path: Path) -> ApplicationOptions:
    return replace(options(tmp_path), extension_paths=(installed_extension(),))


def installed_state_home(tmp_path: Path) -> None:
    """The layout the session expects: a state home for its settings and memory."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)


def messages_of(request: Mapping[str, object]) -> list[dict[str, object]]:
    return list(request["messages"])  # type: ignore[arg-type]


def tool_result_texts(request: Mapping[str, object]) -> list[str]:
    texts: list[str] = []
    for message in messages_of(request):
        if message.get("role") != "toolResult":
            continue
        content = message.get("content")
        if isinstance(content, list):
            texts.append(
                "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
            )
    return texts


def summary_requests(provider: ToolThenReplyProvider) -> list[dict[str, object]]:
    return [request for request in provider.requests if request["system"] == SUMMARIZATION_SYSTEM_PROMPT]


def session_jsonl(tmp_path: Path, session_id: str) -> Path:
    return (
        RunAgentPaths(home=tmp_path / "state").project_session_dir(tmp_path) / f"{session_id}.jsonl"
    )


def compactions_of(session: object) -> list[CompactionEntry]:
    entries = session._entries.values()  # type: ignore[attr-defined]
    return [entry for entry in entries if isinstance(entry, CompactionEntry)]


async def drain(events: AsyncIterator[object]) -> list[object]:
    return [event async for event in events]


async def test_end_to_end_an_oversized_result_is_persisted_while_history_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 rewrites the request the provider sees; the session keeps the full text."""
    installed_state_home(tmp_path)
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "alpha.txt").write_text("A" * 30_000, encoding="utf-8")
    (tmp_path / "beta.txt").write_text("B" * 30_000, encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt", "beta.txt"))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=session_settings(200_000)
    ) as app:
        await drain(app.prompt("read the files"))
        session = app.session
        identity = session.session_id
        assert identity is not None

        texts = tool_result_texts(provider.requests[-1])
        assert all(text.startswith(PERSISTED_OUTPUT_MARKER) for text in texts), (
            "every oversized result left the provider view as a preview"
        )
        assert all("A" * 2_001 not in text for text in texts), "only the 2k preview is kept"
        assert all("B" * 2_001 not in text for text in texts)
        persisted = sorted((tmp_path / ".run" / "tool-results").iterdir())
        assert [path.name for path in persisted] == ["read-0.txt", "read-1.txt"]
        assert persisted[0].read_text(encoding="utf-8") == "A" * 30_000
        assert str(persisted[1]) in texts[1], "the preview names the file that holds the result"

        durable = "".join(message.text for message in session.messages)
        assert "A" * 30_000 in durable, "history keeps the original tool result"
        body = session_jsonl(tmp_path, identity).read_text(encoding="utf-8")
        assert "A" * 30_000 in body
        assert compactions_of(session) == [], "the free layers commit nothing"


async def test_end_to_end_the_manual_command_commits_exactly_one_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/compact <instructions>``: the prompt carries them, the commit is singular."""
    installed_state_home(tmp_path)
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("BETA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt", "beta.txt"))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=session_settings(200_000)
    ) as app:
        session = app.session
        identity = session.session_id
        assert identity is not None
        await drain(app.prompt("read the files"))
        summary_turns = len(summary_requests(provider))

        result = await app.command("/compact focus on the docs")

        assert result.message is not None
        assert result.message.startswith("Prepared a manual summary over")
        assert len(summary_requests(provider)) == summary_turns + 1
        prompt = json.dumps(summary_requests(provider)[-1]["messages"])
        assert "User instructions for summarization:\\nfocus on the docs" in prompt
        assert summary_requests(provider)[-1]["tool_names"] == []

        await drain(app.prompt("and one more turn"))

        compactions = compactions_of(session)
        assert len(compactions) == 1, "exactly one durable compaction is written"
        entry = compactions[0]
        assert "done reading" in entry.summary, "the summary is the model's answer"
        assert "focus on the docs" not in entry.summary, (
            "the instructions shape the prompt, they are not the summary"
        )
        assert entry.first_kept_entry_id is not None
        assert entry.replaces_entry_ids, "the replaced prefix is recorded on the entry"
        assert session._state.messages[0].text.startswith("Previous conversation summary:")
        body = session_jsonl(tmp_path, identity).read_text(encoding="utf-8")
        assert "done reading" in body, "the committed summary is durable"


async def test_end_to_end_a_resumed_session_rebuilds_the_view_without_resummarizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache rebuilds the view: a resumed session neither re-summarizes nor grows."""
    installed_state_home(tmp_path)
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt",))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=session_settings(200_000)
    ) as app:
        identity = app.session.session_id
        assert identity is not None
        await drain(app.prompt("read the file"))
        result = await app.command("/compact")
        assert result.message is not None and result.message.startswith("Prepared a manual summary")
        await drain(app.prompt("and one more turn"))
        assert len(compactions_of(app.session)) == 1

    resumed_provider = ToolThenReplyProvider(())
    resumed = replace(e2e_options(tmp_path), resume=identity)
    async with await CodingApplication.open(
        resumed, provider=resumed_provider, settings=session_settings(200_000)
    ) as app:
        await drain(app.prompt("hello again"))

        first = messages_of(resumed_provider.requests[-1])
        assert first[0]["role"] == "user"
        assert str(first[0]["content"]).startswith("Previous conversation summary:"), (
            "the resumed view is rebuilt from the committed summary"
        )
        assert summary_requests(resumed_provider) == [], (
            "the cache is reused: nothing is summarized again"
        )
        assert compactions_of(app.session)[0].summary is not None


async def test_end_to_end_a_disabled_extension_leaves_requests_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``COMPACTION_LAYER_ENABLED=0`` loads the extension but keeps it silent."""
    installed_state_home(tmp_path)
    monkeypatch.setenv("COMPACTION_LAYER_ENABLED", "0")
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "alpha.txt").write_text("A" * 30_000, encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt",))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=session_settings(200_000)
    ) as app:
        await drain(app.prompt("read the file"))

        assert "A" * 30_000 in json.dumps(messages_of(provider.requests[-1]))
        assert not (tmp_path / ".run" / "tool-results").exists(), "no layer ran"
        assert not any(
            isinstance(entry, CustomEntry) and entry.namespace.startswith("layered_compaction")
            for entry in app.session._entries.values()
        ), "the extension writes nothing while it is off"
        message = await app.session._extension_runtime.execute_command("compact", "")
        assert message is not None and "compaction extension is off" in message


# ------------------------------------------------- the `session_before_compact` gate


_OBSERVER_SOURCE = """
from pathlib import Path

from run_agent_coding.extensions.api import SessionBeforeCompactResult

LOG = Path(__LOG__)
CANCEL = __CANCEL__


def setup(api):
    def record(line):
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(line + "\\n")

    def before_compact(event, context):
        record("before_compact:{}:{}".format(event.reason, event.will_retry))
        if CANCEL:
            return SessionBeforeCompactResult(cancel=True)
        return None

    def compacted(event, context):
        record("compact:{}:{}".format(event.reason, event.from_extension))

    def compact_failed(event, context):
        record("compact_failed:{}:{}".format(event.reason, event.from_extension))

    api.on("session_before_compact", before_compact)
    api.on("session_compact", compacted)
    api.on("session_compact_failed", compact_failed)
"""


def write_observer(tmp_path: Path, *, cancel: bool) -> Path:
    """Install a second extension that observes (and optionally vetoes) compaction."""
    extension = tmp_path / "observer.py"
    source = _OBSERVER_SOURCE.replace("__LOG__", repr(str(tmp_path / "observer.log"))).replace(
        "__CANCEL__", "True" if cancel else "False"
    )
    extension.write_text(source, encoding="utf-8")
    return extension


def observer_log(tmp_path: Path) -> list[str]:
    log = tmp_path / "observer.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def e2e_options_with(tmp_path: Path, *extensions: Path) -> ApplicationOptions:
    return replace(options(tmp_path), extension_paths=(installed_extension(), *extensions))


async def test_end_to_end_the_commit_fires_the_before_compact_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committing fires `session_before_compact`, and its reason is the commit's."""
    installed_state_home(tmp_path)
    observer = write_observer(tmp_path, cancel=False)
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "state" / "MEMORY.md").write_text("Runs pytest with -q", encoding="utf-8")
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt",))

    async with await CodingApplication.open(
        e2e_options_with(tmp_path, observer),
        provider=provider,
        settings=session_settings(200_000),
    ) as app:
        session = app.session
        await drain(app.prompt("read the file"))

        result = await app.command("/compact")
        assert result.message is not None and result.message.startswith("Prepared a manual summary")

        await drain(app.prompt("and one more turn"))

        lines = observer_log(tmp_path)
        gated = [line.split(":")[1] for line in lines if line.startswith("before_compact:")]
        committed = [line.split(":")[1] for line in lines if line.startswith("compact:")]
        assert committed == ["manual"], "the compaction committed under the core reason"
        assert gated and gated[-1] == "manual", (
            "the gate that precedes a commit reports that commit's own reason"
        )
        assert lines[-1] == "compact:manual:True"
        compactions = compactions_of(session)
        assert len(compactions) == 1
        assert "done reading" in compactions[0].summary
        assert "Runs pytest with -q" not in compactions[0].summary, (
            "MEMORY.md is prompt data, never a summary"
        )


async def test_end_to_end_a_cancelled_hook_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A veto on `session_before_compact` writes no `CompactionEntry`, run intact."""
    installed_state_home(tmp_path)
    observer = write_observer(tmp_path, cancel=True)
    monkeypatch.setenv("COMPACTION_LAYER_CONTEXT_WINDOW", "1000")
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt",))
    ui = StubUi()

    async with await CodingApplication.open(
        e2e_options_with(tmp_path, observer),
        provider=provider,
        settings=session_settings(200_000),
    ) as app:
        await app.start(ui=ui)
        session = app.session
        await drain(app.prompt("read the file"))
        result = await app.command("/compact")
        assert result.message is not None and result.message.startswith("Prepared a manual summary")

        events = await drain(app.prompt("and one more turn"))

        assert events[-1].status == "succeeded", "a veto must not fail the run"
        lines = observer_log(tmp_path)
        assert lines, "the gate ran"
        assert all(line == "before_compact:manual:False" for line in lines), (
            "the veto meant no commit and so no `session_compact`"
        )
        assert compactions_of(session) == [], "a cancelled compaction never writes an entry"
        user_texts = [
            message.text for message in session.messages if isinstance(message, UserMessage)
        ]
        assert "read the file" in user_texts and "and one more turn" in user_texts
        assert any(
            level == "warning" and "cancelled the manual compaction" in message
            for message, level in ui.notifications
        ), "the user is told why the session was left uncompacted"
