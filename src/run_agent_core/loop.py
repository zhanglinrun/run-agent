"""Pure Pi-compatible provider/tool agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from time import monotonic_ns
from typing import Literal

from run_agent_core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    ResponseTiming,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from run_agent_core.provider import CancellationToken, ModelProvider
from run_agent_core.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from run_agent_core.tool_history import repair_tool_history
from run_agent_core.tools import AgentTool, AgentToolResult
from run_agent_core.types import JSONValue


@dataclass(frozen=True, slots=True)
class BeforeToolCallResult:
    """Policy decision for one prepared tool call."""

    block: bool = False
    reason: str | None = None
    arguments: Mapping[str, JSONValue] | None = None
    terminate: bool = False


BeforeToolCall = Callable[[ToolCall], Awaitable[BeforeToolCallResult | None]]
AfterToolCall = Callable[
    [ToolCall, AgentToolResult, bool],
    Awaitable[tuple[AgentToolResult, bool]],
]
ToolBatchExecution = Literal["sequential", "parallel"]
TransformContext = Callable[
    [Sequence[AgentMessage], CancellationToken | None],
    Awaitable[Sequence[AgentMessage]],
]


@dataclass(frozen=True, slots=True)
class PrepareNextTurnContext:
    """Completed-turn snapshot passed to runtime policy before another call."""

    message: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    messages: tuple[AgentMessage, ...]
    new_messages: tuple[AgentMessage, ...]


@dataclass(frozen=True, slots=True)
class AgentLoopTurnUpdate:
    """Atomic runtime replacement applied before the next provider request."""

    messages: Sequence[AgentMessage] | None = None
    model: str | None = None
    system: str | None = None
    tools: Sequence[AgentTool] | None = None


PrepareNextTurn = Callable[
    [PrepareNextTurnContext],
    AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None],
]
ShouldStopAfterTurn = Callable[
    [PrepareNextTurnContext],
    bool | Awaitable[bool],
]


@dataclass(frozen=True, slots=True)
class _PreparedToolCall:
    call: ToolCall
    tool: AgentTool
    arguments: Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class _ToolCallOutcome:
    call: ToolCall
    result: AgentToolResult
    is_error: bool


@dataclass(frozen=True, slots=True)
class _ToolQueueItem:
    event: ToolExecutionUpdateEvent | None = None
    outcome: _ToolCallOutcome | None = None


async def run_agent_loop(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    prompts: Sequence[AgentMessage] = (),
    prelude_messages: Sequence[AgentMessage] = (),
    max_turns: int | None = None,
    signal: CancellationToken | None = None,
    session_id: str | None = None,
    get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    tool_execution: ToolBatchExecution = "parallel",
    max_parallel_tools: int = 8,
    prepare_next_turn: PrepareNextTurn | None = None,
    should_stop_after_turn: ShouldStopAfterTurn | None = None,
    transform_context: TransformContext | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop and emit Pi-compatible agent events."""
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    current_model = model
    current_system = system
    current_tools = list(tools)

    yield AgentStartEvent()
    yield TurnStartEvent()
    for message in prelude_messages:
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    invalid_limit = (
        "max_turns must be at least 1"
        if max_turns is not None and max_turns < 1
        else "max_parallel_tools must be at least 1"
        if max_parallel_tools < 1
        else None
    )
    if invalid_limit is not None:
        error = _error_message(current_model, invalid_limit)
        messages.append(error)
        new_messages.append(error)
        yield MessageStartEvent(message=error)
        yield MessageEndEvent(message=error)
        yield TurnEndEvent(message=error)
        yield AgentEndEvent(messages=new_messages)
        return

    turn = 1
    first_turn = True
    pending = tuple(get_steering_messages() if get_steering_messages else ())

    while True:
        has_more_tools = True
        while has_more_tools or pending:
            if not first_turn:
                yield TurnStartEvent()
            first_turn = False

            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            if max_turns is not None and turn > max_turns:
                error = _error_message(
                    current_model,
                    f"Agent stopped after max_turns={max_turns}",
                )
                messages.append(error)
                new_messages.append(error)
                yield MessageStartEvent(message=error)
                yield MessageEndEvent(message=error)
                yield TurnEndEvent(message=error)
                yield AgentEndEvent(messages=new_messages)
                return

            request_messages = messages
            if transform_context is not None:
                request_messages = list(
                    await transform_context(
                        [message.model_copy(deep=True) for message in messages], signal
                    )
                )
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=current_model,
                system=current_system,
                messages=_provider_context(request_messages),
                tools=current_tools,
                signal=signal,
                session_id=session_id,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message

            if assistant is None:
                assistant = _error_message(
                    current_model,
                    "Provider produced no assistant message",
                )
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)

            messages.append(assistant)
            new_messages.append(assistant)
            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(messages=new_messages)
                return

            tool_results: list[ToolResultMessage] = []
            outcomes: list[_ToolCallOutcome] = []
            calls = list(assistant.tool_calls)
            if assistant.stop_reason == "length" and calls:
                async for event in _fail_truncated_tool_calls(calls):
                    yield event
                    _capture_tool_event(event, tool_results, outcomes)
            elif calls:
                async for event in _execute_tool_calls(
                    calls,
                    current_tools,
                    signal,
                    before_tool_call,
                    after_tool_call,
                    tool_execution=tool_execution,
                    max_parallel_tools=max_parallel_tools,
                ):
                    yield event
                    _capture_tool_event(event, tool_results, outcomes)

            for result in tool_results:
                messages.append(result)
                new_messages.append(result)

            has_more_tools = bool(calls) and not (
                outcomes and all(outcome.result.terminate is True for outcome in outcomes)
            )
            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            turn += 1

            turn_context = PrepareNextTurnContext(
                message=assistant,
                tool_results=tuple(tool_results),
                messages=tuple(messages),
                new_messages=tuple(new_messages),
            )
            if prepare_next_turn is not None:
                update = prepare_next_turn(turn_context)
                if isawaitable(update):
                    update = await update
                if update is not None:
                    if update.messages is not None:
                        messages[:] = update.messages
                    if update.model is not None:
                        current_model = update.model
                    if update.system is not None:
                        current_system = update.system
                    if update.tools is not None:
                        current_tools = list(update.tools)

            if should_stop_after_turn is not None:
                should_stop = should_stop_after_turn(turn_context)
                if isawaitable(should_stop):
                    should_stop = await should_stop
                if should_stop:
                    yield AgentEndEvent(messages=new_messages)
                    return

            pending = tuple(get_steering_messages() if get_steering_messages else ())

        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(messages=new_messages)


def _capture_tool_event(
    event: AgentEvent,
    tool_results: list[ToolResultMessage],
    outcomes: list[_ToolCallOutcome],
) -> None:
    if isinstance(event, MessageEndEvent) and isinstance(event.message, ToolResultMessage):
        tool_results.append(event.message)
    elif isinstance(event, ToolExecutionEndEvent):
        outcomes.append(
            _ToolCallOutcome(
                call=ToolCall(
                    id=event.tool_call_id,
                    name=event.tool_name,
                    arguments=event.args if hasattr(event, "args") else {},
                ),
                result=event.result,
                is_error=event.is_error,
            )
        )


def _provider_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Return replayable messages while retaining failures in durable history."""
    replayable = tuple(
        message
        for message in messages
        if not (
            isinstance(message, AssistantMessage)
            and message.stop_reason in {"error", "aborted"}
            and not message.content
        )
    )
    return list(repair_tool_history(replayable).messages)


async def _assistant_events(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None,
    session_id: str | None,
) -> AsyncIterator[AgentEvent]:
    started = False
    provider_elapsed_ns = 0
    first_output_elapsed_ns: int | None = None
    try:
        source: AsyncIterator[AssistantMessageEvent] = provider.stream_response(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            session_id=session_id,
        )
        source_iterator = source.__aiter__()
        while True:
            wait_started_ns = monotonic_ns()
            try:
                event = await anext(source_iterator)
            except StopAsyncIteration:
                break
            provider_elapsed_ns += max(0, monotonic_ns() - wait_started_ns)
            if first_output_elapsed_ns is None and isinstance(
                event,
                (
                    TextDeltaEvent,
                    ThinkingDeltaEvent,
                    ToolCallStartEvent,
                    ToolCallDeltaEvent,
                    ToolCallEndEvent,
                ),
            ):
                first_output_elapsed_ns = provider_elapsed_ns
            if isinstance(event, AssistantStartEvent):
                started = True
                yield MessageStartEvent(message=event.partial)
            elif isinstance(event, AssistantDoneEvent):
                event.message.timing = _response_timing(
                    first_output_elapsed_ns,
                    provider_elapsed_ns,
                )
                if not started:
                    yield MessageStartEvent(message=event.message)
                yield MessageEndEvent(message=event.message)
            elif isinstance(event, AssistantErrorEvent):
                event.error.timing = _response_timing(
                    first_output_elapsed_ns,
                    provider_elapsed_ns,
                )
                if not started:
                    yield MessageStartEvent(message=event.error)
                yield MessageEndEvent(message=event.error)
            else:
                yield MessageUpdateEvent(
                    message=event.partial,
                    assistant_message_event=event,
                )
    except asyncio.CancelledError:
        raise


def _response_timing(
    first_output_elapsed_ns: int | None,
    total_elapsed_ns: int,
) -> ResponseTiming:
    return ResponseTiming(
        time_to_first_output_ms=(
            first_output_elapsed_ns // 1_000_000 if first_output_elapsed_ns is not None else None
        ),
        total_duration_ms=total_elapsed_ns // 1_000_000,
    )


async def _execute_tool_calls(
    calls: Sequence[ToolCall],
    tools: Sequence[AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
    *,
    tool_execution: ToolBatchExecution,
    max_parallel_tools: int,
) -> AsyncIterator[AgentEvent]:
    tool_by_name = {tool.name: tool for tool in tools}
    sequential = tool_execution == "sequential" or any(
        tool_by_name.get(call.name) is not None
        and tool_by_name[call.name].execution_mode == "sequential"
        for call in calls
    )
    if sequential:
        async for event in _execute_tool_calls_sequential(
            calls,
            tool_by_name,
            signal,
            before_tool_call,
            after_tool_call,
        ):
            yield event
        return
    async for event in _execute_tool_calls_parallel(
        calls,
        tool_by_name,
        signal,
        before_tool_call,
        after_tool_call,
        max_parallel_tools=max_parallel_tools,
    ):
        yield event


async def _execute_tool_calls_sequential(
    calls: Sequence[ToolCall],
    tools: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
) -> AsyncIterator[AgentEvent]:
    for call in calls:
        yield _tool_start_event(call)
        prepared, immediate = await _prepare_tool_call(
            call,
            tools,
            signal,
            before_tool_call,
        )
        if immediate is not None:
            outcome = immediate
        else:
            assert prepared is not None
            queue: asyncio.Queue[_ToolQueueItem] = asyncio.Queue()
            task = asyncio.create_task(
                _produce_tool_outcome(prepared, signal, after_tool_call, queue)
            )
            outcome = None
            try:
                while outcome is None:
                    item = await queue.get()
                    if item.event is not None:
                        yield item.event
                    if item.outcome is not None:
                        outcome = item.outcome
                await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert outcome is not None
        yield _tool_end_event(outcome)
        message = _tool_result_message(outcome)
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)
        if signal is not None and signal.is_cancelled():
            break


async def _execute_tool_calls_parallel(
    calls: Sequence[ToolCall],
    tools: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
    *,
    max_parallel_tools: int,
) -> AsyncIterator[AgentEvent]:
    ordered: list[_ToolCallOutcome | _PreparedToolCall] = []
    for call in calls:
        yield _tool_start_event(call)
        prepared, immediate = await _prepare_tool_call(
            call,
            tools,
            signal,
            before_tool_call,
        )
        if immediate is not None:
            outcome = immediate
            ordered.append(outcome)
            yield _tool_end_event(outcome)
        else:
            assert prepared is not None
            ordered.append(prepared)
        if signal is not None and signal.is_cancelled():
            break

    queue: asyncio.Queue[_ToolQueueItem] = asyncio.Queue()
    semaphore = asyncio.Semaphore(max_parallel_tools)

    async def produce(entry: _PreparedToolCall) -> None:
        async with semaphore:
            await _produce_tool_outcome(entry, signal, after_tool_call, queue)

    tasks = [
        asyncio.create_task(produce(entry))
        for entry in ordered
        if isinstance(entry, _PreparedToolCall)
    ]
    remaining = len(tasks)
    completed: dict[str, _ToolCallOutcome] = {}
    try:
        while remaining:
            item = await queue.get()
            if item.event is not None:
                yield item.event
            if item.outcome is not None:
                completed[item.outcome.call.id] = item.outcome
                remaining -= 1
                yield _tool_end_event(item.outcome)
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    finalized = [
        entry if isinstance(entry, _ToolCallOutcome) else completed[entry.call.id]
        for entry in ordered
    ]
    for outcome in finalized:
        message = _tool_result_message(outcome)
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)


async def _prepare_tool_call(
    call: ToolCall,
    tools: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
) -> tuple[_PreparedToolCall | None, _ToolCallOutcome | None]:
    if signal is not None and signal.is_cancelled():
        return None, _ToolCallOutcome(
            call=call,
            result=_error_result("Operation aborted"),
            is_error=True,
        )
    tool = tools.get(call.name)
    if tool is None:
        return None, _ToolCallOutcome(
            call=call,
            result=_error_result(f"Tool {call.name} not found"),
            is_error=True,
        )
    try:
        arguments = (
            tool.prepare_arguments(call.arguments)
            if tool.prepare_arguments is not None
            else call.arguments
        )
        prepared_call = call.model_copy(update={"arguments": dict(arguments)}, deep=True)
        decision = await before_tool_call(prepared_call) if before_tool_call is not None else None
        if signal is not None and signal.is_cancelled():
            return None, _ToolCallOutcome(
                call=call, result=_error_result("Operation aborted"), is_error=True
            )
        if decision is not None and decision.block:
            result = _error_result(decision.reason or "Tool execution was blocked")
            result.terminate = decision.terminate
            return None, _ToolCallOutcome(call=call, result=result, is_error=True)
        if decision is not None and decision.arguments is not None:
            arguments = decision.arguments
            prepared_call = call.model_copy(update={"arguments": dict(arguments)}, deep=True)
    except Exception as exc:  # noqa: BLE001 - preparation and policy failures block the tool
        return None, _ToolCallOutcome(
            call=call,
            result=_error_result(str(exc)),
            is_error=True,
        )
    return _PreparedToolCall(call=prepared_call, tool=tool, arguments=arguments), None


async def _produce_tool_outcome(
    prepared: _PreparedToolCall,
    signal: CancellationToken | None,
    after_tool_call: AfterToolCall | None,
    queue: asyncio.Queue[_ToolQueueItem],
) -> None:
    accepting = True

    def on_update(partial: AgentToolResult) -> None:
        if accepting:
            queue.put_nowait(
                _ToolQueueItem(
                    event=ToolExecutionUpdateEvent(
                        tool_call_id=prepared.call.id,
                        tool_name=prepared.call.name,
                        args=dict(prepared.call.arguments),
                        partial_result=partial.model_copy(deep=True),
                    )
                )
            )

    try:
        try:
            result = await prepared.tool.execute(
                prepared.call.id,
                prepared.arguments,
                signal,
                on_update,
            )
            outcome = _ToolCallOutcome(
                call=prepared.call,
                result=result,
                is_error=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tools are an isolation boundary
            outcome = _ToolCallOutcome(
                call=prepared.call,
                result=_error_result(str(exc)),
                is_error=True,
            )
        outcome = await _finalize_tool_call(outcome, after_tool_call)
        queue.put_nowait(_ToolQueueItem(outcome=outcome))
    finally:
        accepting = False


async def _finalize_tool_call(
    outcome: _ToolCallOutcome,
    after_tool_call: AfterToolCall | None,
) -> _ToolCallOutcome:
    if after_tool_call is None:
        return outcome
    try:
        result, is_error = await after_tool_call(
            outcome.call,
            outcome.result,
            outcome.is_error,
        )
    except Exception as exc:  # noqa: BLE001 - a failed result hook must settle its tool task
        result, is_error = _error_result(str(exc)), True
    return _ToolCallOutcome(call=outcome.call, result=result, is_error=is_error)


async def _fail_truncated_tool_calls(calls: Sequence[ToolCall]) -> AsyncIterator[AgentEvent]:
    for call in calls:
        yield _tool_start_event(call)
        outcome = _ToolCallOutcome(
            call=call,
            result=_error_result(
                f'Tool call "{call.name}" was not executed: the response hit the output '
                "token limit, so its arguments may be truncated. Re-issue the tool call "
                "with complete arguments."
            ),
            is_error=True,
        )
        yield _tool_end_event(outcome)
        message = _tool_result_message(outcome)
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)


def _tool_start_event(call: ToolCall) -> ToolExecutionStartEvent:
    return ToolExecutionStartEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        args=dict(call.arguments),
    )


def _tool_end_event(outcome: _ToolCallOutcome) -> ToolExecutionEndEvent:
    return ToolExecutionEndEvent(
        tool_call_id=outcome.call.id,
        tool_name=outcome.call.name,
        result=outcome.result,
        is_error=outcome.is_error,
    )


def _tool_result_message(outcome: _ToolCallOutcome) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=outcome.call.id,
        tool_name=outcome.call.name,
        content=outcome.result.content,
        details=outcome.result.details,
        added_tool_names=outcome.result.added_tool_names,
        is_error=outcome.is_error,
    )


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})


def _error_message(model: str, message: str) -> AssistantMessage:
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason="error",
        error_message=message,
    )


__all__ = [
    "AfterToolCall",
    "AgentLoopTurnUpdate",
    "BeforeToolCall",
    "BeforeToolCallResult",
    "PrepareNextTurn",
    "PrepareNextTurnContext",
    "ShouldStopAfterTurn",
    "ToolBatchExecution",
    "TransformContext",
    "run_agent_loop",
]
