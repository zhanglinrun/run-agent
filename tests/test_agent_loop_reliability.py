from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from pi_event_helpers import assistant_done, assistant_start, tool_call_end
from run_agent_ai import FakeProvider
from run_agent_core import (
    AgentEvent,
    AgentMessage,
    AgentTool,
    AgentToolResult,
    AssistantMessage,
    ToolCall,
    ToolExecutionEndEvent,
    ToolExecutionUpdateEvent,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.loop import AgentLoopTurnUpdate, PrepareNextTurnContext, run_agent_loop
from run_agent_core.types import JSONValue


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def _tool(name: str, execute_fn, *, mode: str = "parallel") -> AgentTool:  # noqa: ANN001
    return AgentTool(
        name=name,
        label=name,
        description=name,
        parameters={"type": "object"},
        execute_fn=execute_fn,
        execution_mode=mode,  # type: ignore[arg-type]
    )


def _provider_for_calls(calls: list[ToolCall]) -> FakeProvider:
    first = AssistantMessage(content=calls, model="fake")
    final = AssistantMessage(content="done", model="fake")
    return FakeProvider(
        [
            [
                assistant_start(),
                *(tool_call_end(call) for call in calls),
                assistant_done(first, "toolUse"),
            ],
            [assistant_start(), assistant_done(final)],
        ]
    )


@pytest.mark.anyio
async def test_parallel_tools_overlap_and_results_keep_declaration_order() -> None:
    first_release = asyncio.Event()
    second_started = asyncio.Event()

    async def first_execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal
        assert on_update is not None
        on_update(AgentToolResult(content="first update"))
        await asyncio.wait_for(second_started.wait(), timeout=0.5)
        await first_release.wait()
        return AgentToolResult(content="first")

    async def second_execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        second_started.set()
        first_release.set()
        return AgentToolResult(content="second")

    calls = [
        ToolCall(id="one", name="first", arguments={}),
        ToolCall(id="two", name="second", arguments={}),
    ]
    provider = _provider_for_calls(calls)
    messages: list[AgentMessage] = [UserMessage(content="run")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="system",
            messages=messages,
            tools=[_tool("first", first_execute), _tool("second", second_execute)],
        )
    )

    updates = [event for event in events if isinstance(event, ToolExecutionUpdateEvent)]
    assert [event.partial_result.text for event in updates] == ["first update"]
    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert [message.tool_call_id for message in results] == ["one", "two"]
    assert [
        message.tool_call_id
        for message in provider.calls[1][2]
        if isinstance(message, ToolResultMessage)
    ] == ["one", "two"]


@pytest.mark.anyio
async def test_one_sequential_tool_serializes_the_whole_batch() -> None:
    active = 0
    max_active = 0

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        nonlocal active, max_active
        del arguments, signal, on_update
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return AgentToolResult(content=tool_call_id)

    calls = [
        ToolCall(id="one", name="parallel", arguments={}),
        ToolCall(id="two", name="serial", arguments={}),
    ]
    provider = _provider_for_calls(calls)

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="system",
            messages=[UserMessage(content="run")],
            tools=[_tool("parallel", execute), _tool("serial", execute, mode="sequential")],
        )
    )

    assert max_active == 1


@pytest.mark.anyio
async def test_parallel_tool_batch_respects_configured_concurrency_limit() -> None:
    active = 0
    max_active = 0

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        nonlocal active, max_active
        del tool_call_id, arguments, signal, on_update
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return AgentToolResult(content="done")

    calls = [ToolCall(id=f"call-{index}", name="read", arguments={}) for index in range(6)]
    provider = _provider_for_calls(calls)

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="system",
            messages=[UserMessage(content="run")],
            tools=[_tool("read", execute)],
            max_parallel_tools=2,
        )
    )

    assert max_active == 2


@pytest.mark.anyio
async def test_all_terminating_tool_results_stop_before_another_provider_call() -> None:
    async def terminate(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="done", terminate=True)

    calls = [
        ToolCall(id="one", name="stop", arguments={}),
        ToolCall(id="two", name="stop", arguments={}),
    ]
    provider = _provider_for_calls(calls)
    messages: list[AgentMessage] = [UserMessage(content="run")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="system",
            messages=messages,
            tools=[_tool("stop", terminate)],
        )
    )

    assert len(provider.calls) == 1
    assert len([event for event in events if isinstance(event, ToolExecutionEndEvent)]) == 2
    assert [
        message.tool_call_id for message in messages if isinstance(message, ToolResultMessage)
    ] == ["one", "two"]


@pytest.mark.anyio
async def test_prepare_next_turn_atomically_replaces_context_model_system_and_tools() -> None:
    call = ToolCall(id="one", name="old", arguments={})
    first = AssistantMessage(content=[call], model="model-a")
    final = AssistantMessage(content="done", model="model-b")
    provider = FakeProvider(
        [
            [
                assistant_start(model="model-a"),
                tool_call_end(call),
                assistant_done(first, "toolUse"),
            ],
            [assistant_start(model="model-b"), assistant_done(final)],
        ]
    )

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="old result")

    compacted = [UserMessage(content="summary")]
    seen: list[PrepareNextTurnContext] = []

    async def prepare(context: PrepareNextTurnContext) -> AgentLoopTurnUpdate | None:
        seen.append(context)
        if len(seen) > 1:
            return None
        return AgentLoopTurnUpdate(
            messages=compacted,
            model="model-b",
            system="new system",
            tools=[],
        )

    messages: list[AgentMessage] = [UserMessage(content="run")]
    await _collect(
        run_agent_loop(
            provider=provider,
            model="model-a",
            system="old system",
            messages=messages,
            tools=[_tool("old", execute)],
            prepare_next_turn=prepare,
        )
    )

    assert len(seen) == 2
    assert seen[0].message is first
    assert provider.calls[1][0] == "model-b"
    assert provider.calls[1][1] == "new system"
    assert provider.calls[1][2] == compacted
    assert provider.calls[1][3] == []
    assert messages[-1] is final
