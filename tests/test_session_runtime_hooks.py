"""Pi-style extension contracts across real coding sessions and agent turns."""

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from pi_event_helpers import assistant_done, assistant_error
from run_agent_ai import FakeProvider
from run_agent_coding import CodingSession, CodingSessionConfig, RunAgentResourcePaths
from run_agent_coding.extensions import (
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    ContextEvent,
    ContextHookResult,
    ExtensionAPI,
    ExtensionRuntime,
    LoadedExtension,
    ToolCallHookEvent,
    ToolCallHookResult,
    ToolResultHookEvent,
    ToolResultHookResult,
)
from run_agent_core import (
    AgentMessage,
    AgentTool,
    AgentToolResult,
    AssistantMessage,
    CustomMessage,
    ToolCall,
    ToolExecutionEndEvent,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.loop import ToolBatchExecution, run_agent_loop
from run_agent_core.session import JsonlSessionStorage, MessageEntry
from run_agent_core.tools import ToolExecutor
from run_agent_core.types import JSONValue

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _register(runtime: ExtensionRuntime, name: str = "test") -> ExtensionAPI:
    apis: list[ExtensionAPI] = []
    runtime._setup_extension(
        LoadedExtension(name=name, path=None, source_id=f"test:{name}", setup=apis.append)
    )
    return apis[0]


def _tool(name: str, execute: ToolExecutor) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object"},
        execute_fn=execute,
        prompt_snippet=f"Run {name}",
    )


async def _session(
    tmp_path: Path,
    provider: FakeProvider,
    tools: list[AgentTool] | None = None,
    *,
    system: str | None = "base",
) -> CodingSession:
    return await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="fake",
            system=system,
            tools=tools or [],
            cwd=tmp_path,
            storage=JsonlSessionStorage(tmp_path / "session.jsonl"),
            resource_paths=RunAgentResourcePaths(root=tmp_path / "resources", agents_root=None),
            extensions_enabled=False,
            auto_compact_enabled=False,
        )
    )


async def _run(session: CodingSession, prompt: str = "work") -> list[object]:
    return [event async for event in session.prompt(prompt)]


async def test_tool_policy_correlates_calls_and_handles_execution_errors(tmp_path: Path) -> None:
    executed: list[tuple[str, Mapping[str, JSONValue]]] = []
    observed: list[ToolResultHookEvent] = []

    async def execute(call_id, arguments, signal=None, on_update=None):
        executed.append((call_id, dict(arguments)))
        if call_id == "fail":
            raise ValueError("private failure details")
        return AgentToolResult(content="success")

    calls = [ToolCall(id=name, name="work", arguments={}) for name in ("block", "fail", "ok")]
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content=calls), "toolUse")],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    session = await _session(tmp_path, provider, [_tool("work", execute)])
    api = _register(session.extension_runtime)

    def before(event, context):
        assert isinstance(event, ToolCallHookEvent)
        if event.tool_call_id == "block":
            return ToolCallHookResult(block=True, reason="policy")
        return ToolCallHookResult(arguments={"prepared": event.tool_call_id})

    def after(event, context):
        assert isinstance(event, ToolResultHookEvent)
        observed.append(event)
        if event.is_error:
            return ToolResultHookResult(content="redacted failure", details={"redacted": True})
        return None

    api.on("tool_call", before)
    api.on("tool_result", after)
    events = await _run(session)

    assert executed == [("fail", {"prepared": "fail"}), ("ok", {"prepared": "ok"})]
    assert [(event.tool_call_id, event.is_error) for event in observed] == [
        ("fail", True),
        ("ok", False),
    ]
    assert observed[0].arguments == {"prepared": "fail"}
    results = [message for message in session.messages if isinstance(message, ToolResultMessage)]
    assert [message.is_error for message in results] == [True, True, False]
    assert results[1].text == "redacted failure"
    assert results[1].details == {"redacted": True}
    assert [event.is_error for event in events if isinstance(event, ToolExecutionEndEvent)] == [
        True,
        True,
        False,
    ]
    assert "private failure details" not in str(provider.calls[1][2])
    await session.aclose()


async def test_block_with_terminate_does_not_make_another_model_request(tmp_path: Path) -> None:
    async def execute(*args, **kwargs):
        raise AssertionError("blocked tool must never execute")

    call = ToolCall(id="blocked", name="work", arguments={})
    provider = FakeProvider([[assistant_done(AssistantMessage(content=[call]), "toolUse")]])
    session = await _session(tmp_path, provider, [_tool("work", execute)])
    api = _register(session.extension_runtime)
    api.on("tool_call", lambda event, context: ToolCallHookResult(block=True, terminate=True))

    await _run(session)

    assert len(provider.calls) == 1
    assert isinstance(session.messages[-1], ToolResultMessage)
    assert session.messages[-1].is_error is True
    await session.aclose()


@pytest.mark.parametrize("mode", ["parallel", "sequential"])
@pytest.mark.parametrize("phase", ["before", "after"])
async def test_core_hook_errors_settle_instead_of_leaving_a_pending_batch(
    mode: ToolBatchExecution, phase: str
) -> None:
    async def execute(*args, **kwargs):
        return AgentToolResult(content="ok")

    async def before(call):
        if phase == "before":
            raise RuntimeError("before failed")
        return None

    async def after(call, result, is_error):
        if phase == "after":
            raise RuntimeError("after failed")
        return result, is_error

    calls = [ToolCall(id=str(index), name="work", arguments={}) for index in range(2)]
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content=calls), "toolUse")],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    messages: list[AgentMessage] = []

    async def collect():
        return [
            event
            async for event in run_agent_loop(
                provider=provider,
                model="fake",
                system="base",
                messages=messages,
                tools=[_tool("work", execute)],
                tool_execution=mode,
                before_tool_call=before,
                after_tool_call=after,
            )
        ]

    events = await asyncio.wait_for(collect(), timeout=2)

    assert events[-1].type == "agent_end"
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 2
    assert all(message.is_error and message.text == f"{phase} failed" for message in results)


async def test_before_start_overrides_chain_and_are_scoped_to_one_run(tmp_path: Path) -> None:
    provider = FakeProvider([[assistant_done(AssistantMessage(content="done"))] for _ in range(2)])
    session = await _session(tmp_path, provider)
    seen: list[str] = []
    first = _register(session.extension_runtime, "first")
    second = _register(session.extension_runtime, "second")

    def override(event, context):
        assert isinstance(event, BeforeAgentStartEvent)
        seen.append(event.system_prompt)
        if event.prompt == "first":
            return BeforeAgentStartResult(
                system_prompt=event.system_prompt + " first",
                messages=(CustomMessage(custom_type="memory", content="durable context"),),
            )
        return None

    def append(event, context):
        if event.prompt == "first":
            return BeforeAgentStartResult(system_prompt=event.system_prompt + " second")
        return None

    first.on("before_agent_start", override)
    second.on("before_agent_start", append)
    await _run(session, "first")
    assert session.system_prompt == "base"
    await _run(session, "next")

    assert seen == ["base", "base"]
    assert [call[1] for call in provider.calls] == ["base first second", "base"]
    assert [message.role for message in provider.calls[0][2]] == ["user", "custom"]
    entries = await JsonlSessionStorage(tmp_path / "session.jsonl").read_all()
    assert (
        len(
            [
                entry
                for entry in entries
                if isinstance(entry, MessageEntry) and isinstance(entry.message, CustomMessage)
            ]
        )
        == 1
    )
    await session.aclose()


async def test_context_transforms_chain_each_request_without_mutating_history(
    tmp_path: Path,
) -> None:
    async def execute(*args, **kwargs):
        return AgentToolResult(content="tool output")

    call = ToolCall(id="call", name="work", arguments={})
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content=[call]), "toolUse")],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    session = await _session(tmp_path, provider, [_tool("work", execute)])
    broken = _register(session.extension_runtime, "broken")
    first = _register(session.extension_runtime, "first")
    second = _register(session.extension_runtime, "second")
    request_count = 0

    def fail_after_mutation(event, context):
        event.messages[0].content = "must not leak"
        raise RuntimeError("context failed")

    def transform(event, context):
        nonlocal request_count
        assert isinstance(event, ContextEvent)
        request_count += 1
        assert event.messages[0].text == "original"
        event.messages[0].content = "request-only"
        return ContextHookResult(messages=event.messages)

    def append(event, context):
        assert event.messages[0].text == "request-only"
        return ContextHookResult(messages=(*event.messages, UserMessage(content="ephemeral")))

    broken.on("context", fail_after_mutation)
    first.on("context", transform)
    second.on("context", append)
    await _run(session, "original")

    assert request_count == 2
    assert all(call[2][0].text == "request-only" for call in provider.calls)
    assert all(call[2][-1].text == "ephemeral" for call in provider.calls)
    assert session.messages[0].text == "original"
    persisted = (tmp_path / "session.jsonl").read_text(encoding="utf-8")
    assert "request-only" not in persisted
    assert "ephemeral" not in persisted
    assert "must not leak" not in persisted
    assert any(
        "context failed" in diagnostic.message for diagnostic in session.resource_diagnostics
    )
    await session.aclose()


async def test_tools_and_prompt_contributions_refresh_after_a_turn(tmp_path: Path) -> None:
    executed: list[str] = []

    async def execute(call_id, arguments, signal=None, on_update=None):
        executed.append(call_id)
        return AgentToolResult(content="ok")

    calls = [ToolCall(id=name, name=name, arguments={}) for name in ("first", "new")]
    provider = FakeProvider(
        [
            *[[assistant_done(AssistantMessage(content=[call]), "toolUse")] for call in calls],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    session = await _session(tmp_path, provider, [_tool("first", execute)], system=None)
    api = _register(session.extension_runtime)

    def register_after_first_turn(event, context):
        if event.turn_index == 0:
            api.register_tool(_tool("new", execute))
            api.add_prompt_guideline("New tool is now available")

    api.on("turn_end", register_after_first_turn)
    await _run(session)

    assert executed == ["first", "new"]
    assert [tool.name for tool in provider.calls[0][3]] == ["first"]
    assert [tool.name for tool in provider.calls[1][3]] == ["first", "new"]
    assert "New tool is now available" not in provider.calls[0][1]
    assert "New tool is now available" in provider.calls[1][1]
    assert "- new: Run new" in provider.calls[1][1]
    await session.aclose()


async def test_preparation_is_exclusive_and_cancellation_releases_the_session(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([[assistant_done(AssistantMessage(content="done"))]])
    session = await _session(tmp_path, provider)
    api = _register(session.extension_runtime)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_before_start(event, context):
        if event.prompt == "wait":
            entered.set()
            await release.wait()
        return None

    api.on("before_agent_start", wait_before_start)
    task = asyncio.create_task(_run(session, "wait"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        assert session.is_running
        with pytest.raises(RuntimeError, match="already running"):
            await _run(session, "overlap")
        assert provider.calls == []
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not session.is_running
    assert session.system_prompt == "base"
    await _run(session, "next")
    assert len(provider.calls) == 1
    await session.aclose()


async def test_failed_provider_does_not_leak_a_run_prompt_override(tmp_path: Path) -> None:
    provider = FakeProvider(
        [[assistant_error("failed")], [assistant_done(AssistantMessage(content="recovered"))]]
    )
    session = await _session(tmp_path, provider)
    api = _register(session.extension_runtime)
    api.on(
        "before_agent_start",
        lambda event, context: (
            BeforeAgentStartResult(system_prompt="temporary") if event.prompt == "first" else None
        ),
    )

    await _run(session, "first")
    assert not session.is_running
    assert session.system_prompt == "base"
    await _run(session, "next")

    assert [call[1] for call in provider.calls] == ["temporary", "base"]
    await session.aclose()


async def test_adopted_session_rebinds_runtime_callbacks_to_the_live_owner(tmp_path: Path) -> None:
    async def execute(*args, **kwargs):
        return AgentToolResult(content="ok")

    source = await _session(tmp_path / "source", FakeProvider([]))
    call = ToolCall(id="call", name="work", arguments={})
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content=[call]), "toolUse")],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    destination = await _session(
        tmp_path / "destination", provider, [_tool("work", execute)], system=None
    )
    await source._adopt_replacement(destination, reason="new")
    api = _register(source.extension_runtime)
    api.on(
        "before_agent_start",
        lambda event, context: BeforeAgentStartResult(system_prompt="adopted override"),
    )
    api.on(
        "turn_end",
        lambda event, context: (
            api.add_prompt_guideline("Registered by the adopted runtime")
            if event.turn_index == 0
            else None
        ),
    )

    await _run(source)

    assert [call[1] for call in provider.calls] == ["adopted override", "adopted override"]
    assert "Registered by the adopted runtime" in source.system_prompt
    assert "adopted override" not in source.system_prompt
    assert source.messages[-1].text == "done"
    await source.aclose()
