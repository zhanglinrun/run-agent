"""Session event projection following Tau's TUI adapter, without UI dependencies."""

from time import monotonic

from run_agent_coding.events import (
    AgentSettledEvent,
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    QueueUpdateEvent,
    SessionAgentEndEvent,
)
from run_agent_coding.tui.state import ChatItem, ChatRole, TuiState
from run_agent_core.events import (
    AgentEndEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from run_agent_core.messages import AssistantMessage
from run_agent_core.provider_events import (
    TextDeltaEvent,
    TextEndEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
)


class TuiEventAdapter:
    def __init__(self, state: TuiState) -> None:
        self.state = state
        self._blocks: dict[int, ChatItem] = {}

    def apply(self, event: CodingSessionEvent) -> list[ChatItem]:
        """Return changed rows; only agent_settled acknowledges a committed run."""
        state = self.state
        if isinstance(event, AgentStartEvent):
            state.running = True
            state.activity = "Thinking"
            state.started_at = monotonic()
            state.tool_count = state.output_tokens = 0
        elif isinstance(event, QueueUpdateEvent):
            state.queued_steering, state.queued_follow_up = event.steering, event.follow_up
        elif isinstance(event, MessageStartEvent) and isinstance(event.message, AssistantMessage):
            self._blocks.clear()
            state.activity = "Thinking"
        elif isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event
            if isinstance(
                nested, TextDeltaEvent | ThinkingDeltaEvent | TextEndEvent | ThinkingEndEvent
            ):
                role: ChatRole = (
                    "thinking"
                    if isinstance(nested, ThinkingDeltaEvent | ThinkingEndEvent)
                    else "assistant"
                )
                item = self._blocks.get(nested.content_index)
                if item is None:
                    item = state.add(role, pending=True, started_at=monotonic())
                    self._blocks[nested.content_index] = item
                if isinstance(nested, TextDeltaEvent | ThinkingDeltaEvent):
                    item.text += nested.delta
                else:
                    item.text = nested.content
                state.activity = "Thinking" if role == "thinking" else "Writing response"
                return [item]
        elif isinstance(event, MessageEndEvent):
            if isinstance(event.message, AssistantMessage):
                return self._finish_message(event.message)
            return state.project_message(event.message)
        elif isinstance(event, ToolExecutionStartEvent):
            item = state.tool(event.tool_call_id, event.tool_name)
            item.arguments = dict(event.args)
            item.pending = True
            item.started_at = monotonic()
            state.tool_count += 1
            state.activity = f"Running {event.tool_name}"
            return [item]
        elif isinstance(event, ToolExecutionUpdateEvent):
            item = state.tool(event.tool_call_id, event.tool_name)
            if item.pending:
                item.result_text = event.partial_result.text
                return [item]
        elif isinstance(event, ToolExecutionEndEvent):
            item = state.tool(event.tool_call_id, event.tool_name)
            item.result_text = event.result.text
            item.is_error = event.is_error
            item.pending = False
            item.ended_at = monotonic()
            state.activity = "Thinking"
            return [item]
        elif isinstance(event, AgentEndEvent | SessionAgentEndEvent):
            state.activity = "Finishing"
            return state.finish_pending()
        elif isinstance(event, AgentSettledEvent):
            state.running = False
            state.activity = {
                "succeeded": "Ready",
                "cancelled": "Cancelled",
                "failed": "Failed",
                "interrupted": "Interrupted",
                "outcome_unknown": "Outcome unknown",
            }[event.status]
            self._blocks.clear()
            return state.finish_pending()
        elif isinstance(event, CompactionStartEvent):
            state.activity = "Compacting context"
            return [state.add("notice", "Compacting conversation context…")]
        elif isinstance(event, CompactionEndEvent):
            state.activity = "Thinking" if state.running else "Ready"
            if event.error_message:
                return [state.add("error", event.error_message, is_error=True)]
        elif isinstance(event, AutoRetryStartEvent):
            state.activity = f"Retrying ({event.attempt}/{event.max_attempts})"
            return [state.add("notice", f"{state.activity}: {event.error_message}")]
        elif isinstance(event, AutoRetryEndEvent):
            state.activity = "Thinking" if event.success else "Finishing"
        return []

    def _finish_message(self, message: AssistantMessage) -> list[ChatItem]:
        state = self.state
        old = list(self._blocks.values())
        # Error responses may have no canonical content; retain the streamed tail.
        preserve = message.stop_reason in {"error", "aborted"} and not message.content
        if preserve:
            for item in old:
                item.pending = False
                item.ended_at = monotonic()
            changed = [*old, *state.project_message(message)]
        else:
            old_ids = {item.id for item in old}
            insertion = next(
                (index for index, item in enumerate(state.items) if item.id in old_ids),
                len(state.items),
            )
            state.items[:] = [item for item in state.items if item.id not in old_ids]
            changed = state.project_message(message)
            for index, item in enumerate(changed):
                previous = self._blocks.get(index)
                if previous is not None and previous.role == item.role:
                    item.id = previous.id
                    item.started_at = previous.started_at
                    item.ended_at = monotonic()
            new_ids = {item.id for item in changed}
            state.items[:] = [item for item in state.items if item.id not in new_ids]
            state.items[insertion:insertion] = changed
        self._blocks.clear()
        state.output_tokens += message.usage.output
        # Include removed provisional rows so callers know to reconcile their widgets.
        return changed or old
