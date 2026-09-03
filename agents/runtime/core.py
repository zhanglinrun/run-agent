"""Small provider-neutral AgentCore.

This module intentionally has no imports from Harness, policy, verification or
evaluation.  Those concerns are attached through :class:`AgentHooks` and the
tool executor protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..providers.base import ModelRequest, ModelResponse, ProviderAdapter
from .contracts import ToolCall, ToolResult
from .events import EventBus
from .hooks import AgentHooks, ModelContext, NextTurnDecision, NullAgentHooks, ToolCallDecision, TurnResult


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall) -> ToolResult: ...


@dataclass(frozen=True)
class AgentCoreResult:
    text: str
    messages: tuple[dict[str, Any], ...]
    tool_results: tuple[ToolResult, ...]
    turns: int
    usage: dict[str, int]


class AgentCore:
    def __init__(
        self,
        *,
        provider: ProviderAdapter,
        tool_executor: ToolExecutor,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
        hooks: AgentHooks | None = None,
        events: EventBus | None = None,
        max_turns: int = 40,
    ) -> None:
        self.provider = provider
        self.tool_executor = tool_executor
        self.context = ModelContext(system_prompt, [], list(tools or []))
        self.hooks = hooks or NullAgentHooks()
        self.events = events or EventBus()
        self.max_turns = max(1, int(max_turns))
        self._aborted = False

    def abort(self) -> None:
        self._aborted = True

    async def run(self, prompt: str | dict[str, Any], *, max_turns: int | None = None) -> AgentCoreResult:
        self._aborted = False
        initial = {"role": "user", "content": prompt} if isinstance(prompt, str) else dict(prompt)
        self.context.messages.append(initial)
        all_results: list[ToolResult] = []
        answer = ""
        turn_limit = max(1, int(max_turns if max_turns is not None else self.max_turns))
        usage = {"input": 0, "output": 0}
        for turn_index in range(1, turn_limit + 1):
            if self._aborted:
                break
            self.context = await self.hooks.transform_context(self.context)
            await self.events.emit("turn_started", turn=turn_index, message_count=len(self.context.messages))
            await self.events.emit(
                "model_request",
                turn=turn_index,
                messages=tuple(self.context.messages),
                tool_names=tuple(str(item.get("name") or "") for item in self.context.tools),
            )
            response: ModelResponse = await self.provider.complete(
                ModelRequest(tuple(self.context.messages), tuple(self.context.tools), self.context.system)
            )
            await self.events.emit(
                "model_response",
                turn=turn_index,
                text=response.text,
                tool_calls=tuple(response.tool_calls),
                stop_reason=response.stop_reason,
                usage=dict(response.usage),
            )
            usage["input"] += int(response.usage.get("input", response.usage.get("prompt_tokens", 0)) or 0)
            usage["output"] += int(response.usage.get("output", response.usage.get("completion_tokens", 0)) or 0)
            answer = response.text or answer
            assistant: dict[str, Any] = {"role": "assistant", "content": response.text}
            if response.tool_calls:
                assistant["tool_calls"] = [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.input}}
                    for call in response.tool_calls
                ]
            self.context.messages.append(assistant)
            turn_results: list[ToolResult] = []
            for call in response.tool_calls:
                await self.events.emit("tool_started", turn=turn_index, call=call)
                decision: ToolCallDecision = await self.hooks.before_tool_call(call)
                if decision.action != "allow":
                    result = ToolResult(
                        call.id,
                        call.name,
                        decision.message or "Tool call denied.",
                        False,
                        error=decision.message or "denied",
                        executed=False,
                    )
                else:
                    if decision.input is not None:
                        call = ToolCall(call.id, call.name, decision.input)
                    await self.events.emit(
                        "tool_effective", turn=turn_index, call=call
                    )
                    result = await self.tool_executor.execute(call)
                result = await self.hooks.after_tool_call(result)
                await self.events.emit("tool_finished", turn=turn_index, result=result)
                turn_results.append(result)
                all_results.append(result)
                self.context.messages.append({"role": "tool", "tool_call_id": result.call_id, "name": result.name, "content": result.content})
            turn = TurnResult(
                turn_index,
                response.text,
                tuple(turn_results),
                response.stop_reason,
                {
                    "input": int(response.usage.get("input", response.usage.get("prompt_tokens", 0)) or 0),
                    "output": int(response.usage.get("output", response.usage.get("completion_tokens", 0)) or 0),
                },
            )
            await self.events.emit("turn_finished", turn=turn_index, tool_count=len(turn_results))
            if await self.hooks.should_stop_after_turn(turn):
                break
            if not response.tool_calls:
                break
            next_decision: NextTurnDecision = await self.hooks.prepare_next_turn(turn)
            if not next_decision.continue_run:
                break
            if next_decision.message is not None:
                self.context.messages.append(next_decision.message)
        return AgentCoreResult(answer, tuple(self.context.messages), tuple(all_results), turn_index if 'turn_index' in locals() else 0, usage)
