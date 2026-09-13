"""Pure transcript projection, following Tau's display-state/event-adapter split."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from uuid import uuid4

from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    bash_execution_to_text,
)
from run_agent_core.types import JSONValue

ChatRole = Literal["user", "assistant", "thinking", "tool", "notice", "error"]


@dataclass(slots=True)
class ChatItem:
    id: str
    role: ChatRole
    text: str = ""
    tool_name: str = ""
    tool_call_id: str | None = None
    arguments: dict[str, JSONValue] = field(default_factory=dict)
    result_text: str = ""
    pending: bool = False
    is_error: bool = False
    started_at: float | None = None
    ended_at: float | None = None


@dataclass(slots=True)
class TuiState:
    items: list[ChatItem] = field(default_factory=list)
    running: bool = False
    activity: str = "Ready"
    queued_steering: tuple[str, ...] = ()
    queued_follow_up: tuple[str, ...] = ()
    started_at: float | None = None
    tool_count: int = 0
    output_tokens: int = 0
    _tools: dict[str, ChatItem] = field(default_factory=dict, init=False, repr=False)

    def add(
        self,
        role: ChatRole,
        text: str = "",
        *,
        tool_name: str = "",
        tool_call_id: str | None = None,
        arguments: dict[str, JSONValue] | None = None,
        result_text: str = "",
        pending: bool = False,
        is_error: bool = False,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> ChatItem:
        item = ChatItem(
            id=f"item-{uuid4().hex}",
            role=role,
            text=text,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=dict(arguments or {}),
            result_text=result_text,
            pending=pending,
            is_error=is_error,
            started_at=started_at,
            ended_at=ended_at,
        )
        self.items.append(item)
        if role == "tool" and tool_call_id is not None:
            self._tools[tool_call_id] = item
        return item

    def tool(self, call_id: str, name: str) -> ChatItem:
        """Resolve a call in constant time; result-only history is also supported."""
        item = self._tools.get(call_id)
        if item is None:
            item = self.add("tool", name, tool_call_id=call_id, tool_name=name)
        return item

    def finish_pending(self) -> list[ChatItem]:
        """Finish visible partial rows without discarding their text."""
        changed = []
        for item in self.items:
            if item.pending:
                item.pending = False
                item.ended_at = monotonic()
                changed.append(item)
        return changed

    def clear(self) -> None:
        """Clear display rows only; callers own durable session history."""
        self.items.clear()
        self._tools.clear()

    def project_message(self, message: AgentMessage) -> list[ChatItem]:
        """Project canonical content blocks in order, updating existing tool rows."""
        changed: list[ChatItem] = []
        if isinstance(message, UserMessage):
            changed.append(self.add("user", message.text))
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextContent):
                    changed.append(self.add("assistant", block.text))
                elif isinstance(block, ThinkingContent):
                    changed.append(self.add("thinking", block.thinking))
                elif isinstance(block, ToolCall):
                    item = self.tool(block.id, block.name)
                    item.arguments = dict(block.arguments)
                    changed.append(item)
            if message.stop_reason in {"error", "aborted"}:
                changed.append(
                    self.add(
                        "error" if message.stop_reason == "error" else "notice",
                        message.error_message
                        or (
                            "Cancelled"
                            if message.stop_reason == "aborted"
                            else "Model request failed"
                        ),
                        is_error=message.stop_reason == "error",
                    )
                )
        elif isinstance(message, ToolResultMessage):
            item = self.tool(message.tool_call_id, message.tool_name)
            item.result_text = message.text
            item.is_error = message.is_error
            item.pending = False
            changed.append(item)
        elif isinstance(message, CustomMessage):
            if message.display:
                changed.append(self.add("notice", message.text))
        elif isinstance(message, BranchSummaryMessage | CompactionSummaryMessage):
            changed.append(self.add("notice", message.summary))
        elif isinstance(message, BashExecutionMessage):
            changed.append(
                self.add(
                    "tool",
                    message.command,
                    tool_name="bash",
                    result_text=bash_execution_to_text(message),
                    is_error=message.cancelled or message.exit_code not in (None, 0),
                )
            )
        return changed

    def load_messages(self, messages: Sequence[AgentMessage]) -> None:
        """Replace visible history and reset transient execution state."""
        self.clear()
        self.running = False
        self.activity = "Ready"
        self.queued_steering = self.queued_follow_up = ()
        self.started_at = None
        self.tool_count = self.output_tokens = 0
        for message in messages:
            self.project_message(message)
