"""Stable Hook contracts for the provider-neutral AgentCore."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import ToolCall, ToolResult


@dataclass
class ModelContext:
    system: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ToolCallDecision:
    action: str = "allow"
    message: str = ""
    input: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnResult:
    turn: int
    response_text: str
    tool_results: tuple[ToolResult, ...] = ()
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class NextTurnDecision:
    continue_run: bool = True
    message: dict[str, Any] | None = None


class AgentHooks(Protocol):
    async def transform_context(self, context: ModelContext) -> ModelContext: ...
    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision: ...
    async def after_tool_call(self, result: ToolResult) -> ToolResult: ...
    async def should_stop_after_turn(self, turn: TurnResult) -> bool: ...
    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision: ...


class NullAgentHooks:
    async def transform_context(self, context: ModelContext) -> ModelContext:
        return context

    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision:
        return ToolCallDecision()

    async def after_tool_call(self, result: ToolResult) -> ToolResult:
        return result

    async def should_stop_after_turn(self, turn: TurnResult) -> bool:
        return False

    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision:
        return NextTurnDecision(continue_run=True)


class CompositeAgentHooks:
    """Run independent Harness middleware in a deterministic order."""

    def __init__(self, hooks: list[AgentHooks] | tuple[AgentHooks, ...]) -> None:
        self.hooks = tuple(hooks)

    async def transform_context(self, context: ModelContext) -> ModelContext:
        for hook in self.hooks:
            context = await hook.transform_context(context)
        return context

    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision:
        current = call
        for hook in self.hooks:
            decision = await hook.before_tool_call(current)
            if decision.action != "allow":
                return decision
            if decision.input is not None:
                current = ToolCall(current.id, current.name, decision.input)
        return ToolCallDecision(input=current.input if current.input != call.input else None)

    async def after_tool_call(self, result: ToolResult) -> ToolResult:
        for hook in reversed(self.hooks):
            result = await hook.after_tool_call(result)
        return result

    async def should_stop_after_turn(self, turn: TurnResult) -> bool:
        should_stop = False
        for hook in self.hooks:
            should_stop = await hook.should_stop_after_turn(turn) or should_stop
        return should_stop

    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision:
        message = None
        for hook in self.hooks:
            decision = await hook.prepare_next_turn(turn)
            if not decision.continue_run:
                return decision
            message = decision.message or message
        return NextTurnDecision(True, message)
