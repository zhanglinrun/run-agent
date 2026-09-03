"""Core Harness middleware for budgets and append-only session evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from ..execution import WorkspaceJournal
from ..runtime.hooks import ModelContext, NextTurnDecision, ToolCallDecision, TurnResult
from ..runtime.contracts import EventType, ToolCall, ToolResult
from ..session import OperationType, SessionReducer, SessionRepository
from .task import TaskState


class BudgetMiddleware:
    """Charge the shared turn ledger at the actual turn boundary."""

    def __init__(self, state: TaskState) -> None:
        self.state = state

    async def transform_context(self, context: ModelContext) -> ModelContext:
        phase = "repair" if self.state.phase.value == "correcting" else "solve"
        self.state.budgets.ensure_turn_available(phase)
        return context

    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision:
        return ToolCallDecision()

    async def after_tool_call(self, result: ToolResult) -> ToolResult:
        return result

    async def should_stop_after_turn(self, turn: TurnResult) -> bool:
        phase = "repair" if self.state.phase.value == "correcting" else "solve"
        self.state.budgets.consume_turn(phase=phase)
        self.state.budgets.consume_usage(
            input_tokens=int(turn.usage.get("input", 0) or 0),
            output_tokens=int(turn.usage.get("output", 0) or 0),
        )
        remaining = (
            self.state.budgets.repair_remaining
            if phase == "repair"
            else self.state.budgets.solve_remaining
        )
        return remaining <= 0

    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision:
        return NextTurnDecision()


@dataclass
class SessionTaskMiddleware:
    state: TaskState
    repository: SessionRepository
    journal: WorkspaceJournal
    trace: Any | None = None

    def __post_init__(self) -> None:
        self.reducer = SessionReducer(self.repository)
        self._recorded_projection: list[str] = []

    @staticmethod
    def _digest(message: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                message, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()

    def _append_new_messages(self, context: ModelContext) -> None:
        current = [self._digest(message) for message in context.messages]
        prefix = 0
        limit = min(len(current), len(self._recorded_projection))
        while prefix < limit and current[prefix] == self._recorded_projection[prefix]:
            prefix += 1
        for message in context.messages[prefix:]:
            self.reducer.append_message(
                self.state.session_id, message, lane_id=self.state.lane_id
            )
        self._recorded_projection = current

    def remember_messages(self, messages: list[dict[str, Any]]) -> None:
        self._recorded_projection = [self._digest(message) for message in messages]

    def remember_projection(self, messages: list[dict[str, Any]]) -> None:
        """Mark a rewritten projection already persisted by an extension."""
        self.remember_messages(messages)

    def flush_context(self, context: ModelContext) -> None:
        self._append_new_messages(context)

    async def transform_context(self, context: ModelContext) -> ModelContext:
        self._append_new_messages(context)
        self.repository.append_operation(
            self.state.session_id,
            self.state.lane_id,
            OperationType.TURN_STARTED,
            {"message_count": len(context.messages)},
            run_id=self.state.run_id,
        )
        return context

    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision:
        if self.trace is not None:
            self.trace.emit(
                EventType.TOOL_REQUESTED,
                call_id=call.id,
                name=call.name,
                input=call.input,
            )
        self.repository.append_operation(
            self.state.session_id,
            self.state.lane_id,
            OperationType.TOOL_STARTED,
            {"call_id": call.id, "name": call.name, "input": call.input},
            run_id=self.state.run_id,
        )
        return ToolCallDecision()

    async def after_tool_call(self, result: ToolResult) -> ToolResult:
        changed = self.journal.observe()
        self.state.changes.update(changed)
        self.repository.append_operation(
            self.state.session_id,
            self.state.lane_id,
            OperationType.TOOL_FINISHED,
            {
                "call_id": result.call_id,
                "name": result.name,
                "ok": result.ok,
                "changed_paths": list(changed),
            },
            run_id=self.state.run_id,
        )
        if self.trace is not None:
            self.trace.emit(
                EventType.TOOL_RESULT,
                call_id=result.call_id,
                name=result.name,
                ok=result.ok,
                executed=result.executed,
                content=result.content,
                error=result.error,
            )
        return result

    async def should_stop_after_turn(self, turn: TurnResult) -> bool:
        self.repository.append_operation(
            self.state.session_id,
            self.state.lane_id,
            OperationType.TURN_FINISHED,
            {
                "turn": turn.turn,
                "tool_count": len(turn.tool_results),
                "stop_reason": turn.stop_reason,
            },
            run_id=self.state.run_id,
        )
        return False

    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision:
        return NextTurnDecision()


__all__ = ["BudgetMiddleware", "SessionTaskMiddleware"]
