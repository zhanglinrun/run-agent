"""Hook-pipeline and end-to-end tests for the four-layer compaction extension.

Two levels, both offline:

* the extension is driven directly through its registered handlers with stub host
  objects (the shape ``run_agent_extensions.hermes_memory``'s tests use), which is
  where the layer order, the one-commit-per-request rule, the breaker and the
  reactive latch are pinned;
* the end-to-end tests load the real package into a real ``CodingApplication`` with
  a fake provider and assert what the provider actually received, while the session
  JSONL history stays untouched.
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
    InputEvent,
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
from run_agent_extensions.claude_compaction import (
    SNIP_NUDGE_TEXT,
    SNIP_TOOL_NAME,
    TIME_BASED_MC_CLEARED_MESSAGE,
    FourLayerCompaction,
    PreparedSummary,
    SnipBoundary,
    setup,
)
from run_agent_extensions.claude_compaction.state import SNIP_BOUNDARY_TEXT

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "run_agent_extensions" / "claude_compaction"
)
FOUR_LAYER_SETTINGS = {"compaction": {"strategy": "four-layer"}}
NOW_MS = 1_700_000_000_000

EXPECTED_HOOKS = {
    "session_start",
    "before_agent_start",
    "before_provider_request",
    "after_provider_response",
    "message_end",
    "input",
    "agent_settled",
    "session_compact",
    "session_compact_failed",
}

# A threshold a small test view crosses: 1% of a 50k effective window.
LOW_THRESHOLD_ENV = {
    "COMPACTION_FOUR_LAYER_CONTEXT_WINDOW": "60000",
    "COMPACTION_FOUR_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY": "10000",
    "COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE": "1",
}


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

    @property
    def available(self) -> bool:
        return self._available

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.calls += 1
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
    ) -> None:
        self.paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")
        self.cwd = tmp_path
        self.environment = dict(environment or {})
        self.session_id = session_id
        self.current_snapshot_id = snapshot_id
        self.transcript: tuple[object, ...] = tuple(transcript)
        self.ui = ui or StubUi()
        self.has_ui = self.ui.has_ui
        self.services = StubServices(
            inference=inference or StubInference(),
            snapshots=snapshots or StubSnapshots(("e0", "e1", "e2", "e3")),
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


def write_settings(tmp_path: Path, strategy: str) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"compaction": {"strategy": strategy}}), encoding="utf-8"
    )


async def started(
    tmp_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    strategy: str = "four-layer",
    inference: StubInference | None = None,
    snapshots: StubSnapshots | None = None,
    transcript: Sequence[object] = (),
    snapshot_id: str | None = "snap-current",
) -> tuple[StubApi, FourLayerCompaction, StubContext]:
    write_settings(tmp_path, strategy)
    context = StubContext(
        tmp_path,
        environment=environment,
        inference=inference,
        snapshots=snapshots,
        transcript=transcript,
        snapshot_id=snapshot_id,
    )
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]
    await fire(api, "session_start", SessionStartEvent(reason="startup"), context)
    return api, extension_of(api), context


def extension_of(api: StubApi) -> FourLayerCompaction:
    """The instance ``setup`` bound its handlers to."""
    handler = api.handlers["before_provider_request"][0]
    assert isinstance(handler.__self__, FourLayerCompaction)  # type: ignore[attr-defined]
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


# --------------------------------------------------------------- registration


def test_setup_registers_the_hook_tool_and_command_surface(tmp_path: Path) -> None:
    context = StubContext(tmp_path)
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]

    assert set(api.handlers) == EXPECTED_HOOKS
    assert set(api.tools) == {SNIP_TOOL_NAME}
    assert set(api.commands) == {"force-snip", "four-layer-compact"}
    assert api.tools[SNIP_TOOL_NAME].execution_mode == "sequential"
    assert api.guidelines and "force-snip" in api.guidelines[0]


# ------------------------------------------------------------------- pipeline


def big_view() -> list[object]:
    """A view whose estimate crosses the 1%-of-50k test threshold."""
    return view_with_tools(2, size=4_000)


async def test_pipeline_runs_l1_then_l2_keeps_pairing_and_the_prefix(tmp_path: Path) -> None:
    api, extension, context = await started(
        tmp_path,
        environment={
            "COMPACTION_FOUR_LAYER_CACHED_TRIGGER_THRESHOLD": "1",
            "COMPACTION_FOUR_LAYER_KEEP_RECENT": "1",
        },
    )
    view = view_with_tools(3, size=400)
    # /force-snip marks the prefix and the first round only, so the later rounds
    # stay in the view and make the L1 rewrite observable.
    context.transcript = tuple(view[:3])
    await command(api, "force-snip")

    outcome = await request_through(api, context, view)

    assert outcome is not None
    assert outcome.compaction is None, "no summary was prepared, so nothing is committed"
    rewritten = list(outcome.request.messages)
    texts = [message.text for message in rewritten]
    assert texts[0] == "first question", "the cacheable prefix survives"
    assert SNIP_BOUNDARY_TEXT in texts
    assert TIME_BASED_MC_CLEARED_MESSAGE in texts, "L1 cleared the second round's result"
    assert texts[-1] == "x" * 400, "the most recent result survives L1"
    assert not any("x" * 100 in text and text.endswith("x" * 400) for text in texts[:-1])
    assert outcome.request.system == "SYS"
    assert extension.state.strategy_seen == "four-layer"
    # Pairing is legal: every assistant tool call is followed by its result.
    for index, message in enumerate(rewritten):
        if not isinstance(message, AssistantMessage):
            continue
        for offset, tool_call in enumerate(message.tool_calls, start=1):
            following = rewritten[index + offset]
            assert isinstance(following, ToolResultMessage)
            assert following.tool_call_id == tool_call.id


async def test_pipeline_leaves_the_request_alone_outside_the_four_layer_strategy(
    tmp_path: Path,
) -> None:
    api, extension, context = await started(
        tmp_path,
        strategy="cheap-first",
        environment={"COMPACTION_FOUR_LAYER_CACHED_TRIGGER_THRESHOLD": "1"},
    )
    view = view_with_tools(3)

    assert await request_through(api, context, view) is None
    assert extension.rewrite_enabled is False
    assert extension.state.strategy_seen == "cheap-first"
    # The surfaces stay registered and say why nothing happened.
    assert "four-layer" in (await command(api, "force-snip") or "")
    assert "four-layer" in (await command(api, "four-layer-compact") or "")


async def test_pipeline_nudges_the_model_at_thirty_messages(tmp_path: Path) -> None:
    api, _extension, context = await started(tmp_path)
    view = [user(f"message {index}") for index in range(30)]

    outcome = await request_through(api, context, view)

    assert outcome is not None
    assert list(outcome.request.messages)[-1].text == SNIP_NUDGE_TEXT
    assert len(outcome.request.messages) == 31
    short = await request_through(api, context, [user("only")])
    assert short is None


async def test_the_input_hook_surfaces_the_nudge_once_per_view_size(tmp_path: Path) -> None:
    api, _extension, context = await started(tmp_path)
    await request_through(api, context, [user(f"message {index}") for index in range(30)])

    await fire(api, "input", InputEvent(text="hi"), context)
    await fire(api, "input", InputEvent(text="again"), context)

    assert api.notifications == [(SNIP_NUDGE_TEXT, "info")]


# ------------------------------------------------------------------- commits


async def test_a_prepared_summary_produces_exactly_one_commit(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_THRESHOLD_ENV)
    view = view_with_tools(2)
    summary = PreparedSummary(
        text="Summary:\nthe earlier portion",
        trigger="auto",
        tokens_before=123,
        replaced_rows=1,
        created_at=1.0,
        anchor_key="",
        layer="L4",
    )
    extension.state.record_prepared(summary, None)

    outcome = await request_through(api, context, view)

    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None
    assert commit.trigger == "auto"
    assert commit.summary == summary.text
    assert commit.tokens_before > 0
    assert commit.first_kept_entry_id == "e1", "the entry id comes from the snapshot payload"
    assert commit.metadata is not None and commit.metadata["layer"] == "L4"
    assert list(outcome.request.messages)[0].text == summary.text
    assert context.services.snapshots.reads == ["snap-current"]

    # One commit per request: the latch refuses a second request in the same hook run.
    second = await extension._commit_for(context, summary, tokens_before=5)
    assert second is None


async def test_a_commit_is_skipped_with_a_diagnostic_without_an_entry_id_snapshot(
    tmp_path: Path,
) -> None:
    api, extension, context = await started(
        tmp_path, environment=LOW_THRESHOLD_ENV, snapshot_id=None
    )
    summary = PreparedSummary(
        text="Summary:\nx",
        trigger="auto",
        tokens_before=10,
        replaced_rows=1,
        created_at=1.0,
        layer="L4",
    )
    extension.state.record_prepared(summary, None)

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None
    assert outcome.compaction is None
    assert any("no durable commit" in note for note in extension.state.notes)


async def test_l4_defers_on_a_foreground_run_without_tripping_the_breaker(tmp_path: Path) -> None:
    inference = StubInference(busy=True)
    api, extension, context = await started(
        tmp_path, environment=LOW_THRESHOLD_ENV, inference=inference
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
        tmp_path, environment=LOW_THRESHOLD_ENV, inference=inference
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
        tmp_path, environment=LOW_THRESHOLD_ENV, inference=inference
    )
    extension.state.record_prepared(
        PreparedSummary(
            text="Summary:\ncached",
            trigger="auto",
            tokens_before=9,
            replaced_rows=1,
            created_at=1.0,
            layer="L4",
        ),
        None,
    )

    outcome = await request_through(api, context, view_with_tools(2))

    assert inference.calls == 0
    assert outcome is not None and outcome.compaction is not None
    assert list(outcome.request.messages)[0].text == "Summary:\ncached"


# ------------------------------------------------------------------ reactive


async def test_reactive_attempts_once_per_run_regardless_of_the_threshold(tmp_path: Path) -> None:
    inference = StubInference(text="<summary>emergency</summary>")
    api, extension, context = await started(tmp_path, inference=inference)
    view = view_with_tools(2)

    # The default window means the proactive threshold is far away.
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
    api, extension, context = await started(tmp_path, inference=inference)

    await fire(
        api, "after_provider_response", AfterProviderResponseEvent(status=400, headers={}), context
    )
    assert extension.state.reactive_armed is False, "a 400 alone proves nothing"
    assert await request_through(api, context, view_with_tools(2)) is None
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
    assert await request_through(api, context, view_with_tools(2)) is not None
    assert inference.calls == 1


# ------------------------------------------------------------------------ L3


async def test_l3_reuses_memory_files_without_a_model_call(tmp_path: Path) -> None:
    inference = StubInference(errors=[AssertionError("L3 must not call the model")])
    environment = {
        "COMPACTION_FOUR_LAYER_CONTEXT_WINDOW": "40000",
        "COMPACTION_FOUR_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY": "20000",
        "COMPACTION_FOUR_LAYER_SM_MIN_TOKENS": "2000",
        "COMPACTION_FOUR_LAYER_SM_MIN_TEXT_BLOCK_MESSAGES": "0",
    }
    api, _extension, context = await started(tmp_path, environment=environment, inference=inference)
    (context.paths.home / "MEMORY.md").write_text("Runs pytest with -q", encoding="utf-8")
    view = [
        user("q" * 24_000),
        assistant("answer", ToolCall(id="call-0", name="read", arguments={})),
        result("call-0", "x" * 400),
        user("y" * 8_000),
    ]

    outcome = await request_through(api, context, view)

    assert inference.calls == 0
    assert outcome is not None
    head = list(outcome.request.messages)[0]
    assert "Runs pytest with -q" in head.text
    assert "The summary below covers the earlier portion" in head.text
    assert "Recent messages are preserved verbatim." in head.text
    commit = outcome.compaction
    assert commit is not None and commit.trigger == "auto"
    assert commit.metadata is not None and commit.metadata["layer"] == "L3"


# ------------------------------------------------------------------ surfaces


async def test_the_snip_tool_writes_a_durable_boundary(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path)
    context.transcript = tuple(view_with_tools(2))

    tool_result = await api.tools[SNIP_TOOL_NAME].execute("call-snip", {"keep_recent": 2})

    assert "Snipped 3 messages" in tool_result.text
    assert len(api.entries) == 1
    entry_id, namespace, data = api.entries[0]
    assert namespace == "claude_compaction.snip"
    boundary = SnipBoundary.from_payload(data)
    assert boundary is not None
    assert boundary.trigger == "snip"
    assert boundary.tokens_freed > 0
    assert len(boundary.removed) == 3
    assert extension.state.boundary_entry_ids == [entry_id]
    assert tool_result.details is not None and tool_result.details["snipped"] == 3


async def test_the_snip_tool_writes_nothing_when_it_is_turned_off(tmp_path: Path) -> None:
    api, _extension, context = await started(tmp_path, strategy="cheap-first")
    context.transcript = tuple(view_with_tools(2))

    tool_result = await api.tools[SNIP_TOOL_NAME].execute("call-snip", {})

    assert "four-layer" in tool_result.text
    assert api.entries == []


async def test_force_snip_command_marks_the_whole_history(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path)
    context.transcript = tuple(view_with_tools(2))

    message = await command(api, "force-snip")

    assert message is not None and message.startswith("Snipped 5 message(s).")
    boundary = SnipBoundary.from_payload(api.entries[0][2])
    assert boundary is not None and boundary.trigger == "force-snip"
    assert len(extension.state.removed_keys) == 5

    context.transcript = ()
    assert await command(api, "force-snip") == "No messages to snip."


async def test_the_manual_command_prepares_a_summary_the_next_request_commits(
    tmp_path: Path,
) -> None:
    inference = StubInference(text="<summary>manual work</summary>")
    api, extension, context = await started(tmp_path, inference=inference)
    context.transcript = tuple(view_with_tools(2))
    await request_through(api, context, view_with_tools(2))
    context.current_snapshot_id = "snap-current"

    message = await command(api, "four-layer-compact")

    assert message is not None and message.startswith("Prepared a manual L4 summary over")
    assert inference.calls == 1
    assert extension.state.prepared is not None
    assert api.entries[0][1] == "claude_compaction.summary"

    outcome = await request_through(api, context, view_with_tools(2))

    assert outcome is not None
    commit = outcome.compaction
    assert commit is not None and commit.trigger == "manual"
    assert "Summary:\nmanual work" in commit.summary


async def test_agent_settled_prepares_a_summary_while_the_session_is_idle(tmp_path: Path) -> None:
    inference = StubInference(text="<summary>idle summary</summary>")
    environment = {**LOW_THRESHOLD_ENV, "COMPACTION_FOUR_LAYER_INLINE_MODEL_ATTEMPT": "0"}
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


async def test_session_compact_events_reconcile_the_state(tmp_path: Path) -> None:
    api, extension, context = await started(tmp_path, environment=LOW_THRESHOLD_ENV)
    extension.state.record_prepared(
        PreparedSummary(
            text="Summary:\ncommitted",
            trigger="auto",
            tokens_before=5,
            replaced_rows=1,
            created_at=1.0,
            anchor_key="cc:anchor",
            layer="L4",
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
    assert extension.state.last_summarized_key == "cc:anchor"
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

    The tool-list gate keeps the session-naming call (which passes no tools) from
    consuming a read, and the counter keeps the fake finite whatever the
    extension does to the view.
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


def four_layer_settings(context_window: int) -> ProviderSettings:
    return ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("test",),
                default_model="test",
                api_key_env="CLAUDE_COMPACTION_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def installed_extension() -> Path:
    assert (PACKAGE_DIR / "extension.py").is_file()
    return PACKAGE_DIR


def e2e_options(tmp_path: Path) -> ApplicationOptions:
    return replace(options(tmp_path), extension_paths=(installed_extension(),))


def installed_four_layer_settings(tmp_path: Path) -> None:
    """The session the extension needs: `compaction.strategy = "four-layer"`."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(FOUR_LAYER_SETTINGS), encoding="utf-8")


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


def session_jsonl(tmp_path: Path, session_id: str) -> Path:
    return (
        RunAgentPaths(home=tmp_path / "state").project_session_dir(tmp_path) / f"{session_id}.jsonl"
    )


async def drain(events: AsyncIterator[object]) -> list[object]:
    return [event async for event in events]


async def test_end_to_end_the_provider_view_is_compacted_while_history_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request is L1+L2 processed; the session file and transcript are not."""
    installed_four_layer_settings(tmp_path)
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_CACHED_TRIGGER_THRESHOLD", "1")
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_KEEP_RECENT", "1")
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("BETA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt", "beta.txt"))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=four_layer_settings(200_000)
    ) as app:
        await drain(app.prompt("read the files"))
        session = app.session
        identity = session.session_id
        assert identity is not None
        # The provider actually received both rounds with the older result cleared.
        assert tool_result_texts(provider.requests[-1]) == [
            TIME_BASED_MC_CLEARED_MESSAGE,
            "BETA-CONTENT",
        ], "L1 must rewrite the request the provider sees"
        assert [message["role"] for message in messages_of(provider.requests[-1])] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
            "toolResult",
        ]
        durable = [message.text for message in session.messages]
        assert "ALPHA-CONTENT" in "".join(durable), "history keeps the original tool result"
        jsonl = session_jsonl(tmp_path, identity)
        assert jsonl.is_file()

        requests_before_snip = len(provider.requests)
        result = await app.command("/force-snip")
        assert result.message is not None and result.message.startswith("Snipped")
        await drain(app.prompt("after the snip"))

        first_after_snip = provider.requests[requests_before_snip]
        joined = json.dumps(messages_of(first_after_snip))
        assert "ALPHA-CONTENT" not in joined, "snipped content leaves the view"
        assert "BETA-CONTENT" not in joined
        assert "done reading" not in joined
        assert SNIP_BOUNDARY_TEXT in joined, "the boundary text is injected instead"
        assert "after the snip" in joined
        roles = [message["role"] for message in messages_of(first_after_snip)]
        assert roles == ["user", "user", "user"], "prefix, boundary text and the new prompt"
        assert TIME_BASED_MC_CLEARED_MESSAGE not in joined, "nothing is left to clear"

        # The durable transcript is append-only, and every earlier message is
        # still there in its original order.
        roles = [message.role for message in session.messages]
        assert roles[:7] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
            "toolResult",
            "assistant",
            "user",
        ]
        assert session.messages[6].text == "after the snip"
        assert len(roles) > 7, "the run's own messages are durable too"
        body = jsonl.read_text(encoding="utf-8")
        assert "ALPHA-CONTENT" in body and "BETA-CONTENT" in body
        assert not any(isinstance(entry, CompactionEntry) for entry in session._entries.values()), (
            "no compaction entry is written for a snip"
        )


async def test_end_to_end_l3_commits_a_memory_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3 summarizes the prefix from memory files and the core commits it."""
    installed_four_layer_settings(tmp_path)
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_CONTEXT_WINDOW", "60000")
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY", "10000")
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE", "1")
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_SM_MIN_TOKENS", "0")
    monkeypatch.setenv("COMPACTION_FOUR_LAYER_SM_MIN_TEXT_BLOCK_MESSAGES", "0")
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    (home / "MEMORY.md").write_text("Runs pytest with -q", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt", "beta.txt"))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=four_layer_settings(200_000)
    ) as app:
        session = app.session
        identity = session.session_id
        assert identity is not None
        await drain(app.prompt("read the files"))
        assert not any(isinstance(entry, CompactionEntry) for entry in session._entries.values())

        await drain(app.prompt("y" * 2_000))

        compactions = [
            entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)
        ]
        assert len(compactions) == 1, "L3 must commit exactly one compaction"
        entry = compactions[0]
        assert "Runs pytest with -q" in entry.summary
        assert entry.first_kept_entry_id is not None
        assert session._state.messages[0].text.startswith("Previous conversation summary:")
        texts = [message.text for message in session._state.messages]
        assert any("after the snip" not in text for text in texts)
        # The durable file records the summary, and the run still succeeded.
        body = session_jsonl(tmp_path, identity).read_text(encoding="utf-8")
        assert "Runs pytest with -q" in body


async def test_end_to_end_a_foreign_strategy_leaves_requests_untouched(tmp_path: Path) -> None:
    """With ``cheap-first`` the extension is loaded but silent."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"compaction": {"strategy": "cheap-first"}}), encoding="utf-8"
    )
    (tmp_path / "alpha.txt").write_text("ALPHA-CONTENT", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("BETA-CONTENT", encoding="utf-8")
    provider = ToolThenReplyProvider(("alpha.txt", "beta.txt"))

    async with await CodingApplication.open(
        e2e_options(tmp_path), provider=provider, settings=four_layer_settings(200_000)
    ) as app:
        await drain(app.prompt("read the files"))

        last = provider.requests[-1]
        assert TIME_BASED_MC_CLEARED_MESSAGE not in json.dumps(messages_of(last))
        assert "ALPHA-CONTENT" in json.dumps(messages_of(last))
        assert not any(
            isinstance(entry, CustomEntry) and entry.namespace.startswith("claude_compaction")
            for entry in app.session._entries.values()
        ), "the surfaces write nothing while the strategy is foreign"
        assert (await app.command("/force-snip")).message is not None
        message = await app.session._extension_runtime.execute_command("force-snip", "")
        assert message is not None and "four-layer" in message
