"""Approximate context-size estimation for Run Agent coding sessions."""

from __future__ import annotations

from dataclasses import dataclass

from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    ThinkingContent,
    ToolResultMessage,
    message_text,
)
from run_agent_core.tools import AgentTool

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_OVERHEAD_TOKENS = 16
SUMMARY_MESSAGE_CHAR_LIMIT = 500
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
COMPACTION_SUMMARY_PREFIX = "Previous conversation summary:\n"


@dataclass(frozen=True, slots=True)
class ContextUsageEstimate:
    """Best available context-size accounting for one provider request."""

    total_tokens: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    message_count: int
    tool_count: int
    provider_tokens: int = 0
    trailing_tokens: int = 0

    @property
    def uses_provider_usage(self) -> bool:
        """Return whether a provider-reported usage block anchors this estimate."""
        return self.provider_tokens > 0


def estimate_text_tokens(text: str) -> int:
    """Return a deterministic rough token estimate for text."""
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_message_tokens(message: AgentMessage) -> int:
    """Return a rough token estimate for one provider-neutral message."""
    tokens = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(message_text(message))
    if isinstance(message, AssistantMessage):
        tokens += sum(
            estimate_text_tokens(block.thinking)
            for block in message.content
            if isinstance(block, ThinkingContent)
        )
        tokens += sum(
            estimate_text_tokens(call.name) + estimate_text_tokens(str(call.arguments))
            for call in message.tool_calls
        )
    elif isinstance(message, ToolResultMessage):
        tokens += estimate_text_tokens(message.tool_name)
    return tokens


def estimate_tool_tokens(tool: AgentTool) -> int:
    """Return a rough token estimate for one tool definition."""
    return (
        TOOL_OVERHEAD_TOKENS
        + estimate_text_tokens(tool.name)
        + estimate_text_tokens(tool.description)
        + estimate_text_tokens(str(tool.input_schema))
    )



def provider_context_tokens(message: AssistantMessage) -> int:
    """Return the provider-reported context represented by an assistant response."""
    usage = message.usage
    return usage.total_tokens or (usage.input + usage.output + usage.cache_read + usage.cache_write)


def _last_applicable_provider_usage(
    messages: tuple[AgentMessage, ...],
) -> tuple[int, int] | None:
    """Find the latest valid usage block that still describes the active prefix.

    A newer prefix message can be inserted by compaction or history rewriting. In that
    case an older assistant's usage describes the pre-rewrite context and must not be
    reused.
    """
    latest_prefix_timestamp = -1
    usage_info: tuple[int, int] | None = None
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            tokens = provider_context_tokens(message)
            if (
                message.timestamp >= latest_prefix_timestamp
                and message.stop_reason not in {"aborted", "error"}
                and tokens > 0
            ):
                usage_info = (index, tokens)
        latest_prefix_timestamp = max(latest_prefix_timestamp, message.timestamp)
    return usage_info


def estimate_context_usage(
    *,
    system: str,
    messages: tuple[AgentMessage, ...],
    tools: tuple[AgentTool, ...],
) -> ContextUsageEstimate:
    """Return provider-anchored context accounting with a deterministic fallback.

    Provider usage is authoritative for the prefix represented by the latest successful
    assistant response. Only messages and dynamically added tools after that response
    are estimated. Without applicable usage, the whole request uses the character-based
    fallback.
    """
    usage_info = _last_applicable_provider_usage(messages)
    if usage_info is not None:
        usage_index, provider_tokens = usage_info
        trailing_messages = messages[usage_index + 1 :]
        trailing_message_tokens = sum(
            estimate_message_tokens(message) for message in trailing_messages
        )
        added_tool_names = {
            name
            for message in trailing_messages
            if isinstance(message, ToolResultMessage) and message.added_tool_names
            for name in message.added_tool_names
        }
        added_tool_tokens = sum(
            estimate_tool_tokens(tool) for tool in tools if tool.name in added_tool_names
        )
        trailing_tokens = trailing_message_tokens + added_tool_tokens
        return ContextUsageEstimate(
            total_tokens=provider_tokens + trailing_tokens,
            system_tokens=0,
            message_tokens=trailing_message_tokens,
            tool_tokens=added_tool_tokens,
            message_count=len(messages),
            tool_count=len(tools),
            provider_tokens=provider_tokens,
            trailing_tokens=trailing_tokens,
        )

    system_tokens = estimate_text_tokens(system)
    message_tokens = sum(estimate_message_tokens(message) for message in messages)
    tool_tokens = sum(estimate_tool_tokens(tool) for tool in tools)
    return ContextUsageEstimate(
        total_tokens=system_tokens + message_tokens + tool_tokens,
        system_tokens=system_tokens,
        message_tokens=message_tokens,
        tool_tokens=tool_tokens,
        message_count=len(messages),
        tool_count=len(tools),
        trailing_tokens=system_tokens + message_tokens + tool_tokens,
    )


def summarize_messages_for_compaction(messages: tuple[AgentMessage, ...]) -> str:
    """Build a deterministic compact summary from provider-neutral messages."""
    if not messages:
        return "No prior messages."
    lines = [f"Automatically compacted {len(messages)} prior message(s)."]
    for index, message in enumerate(messages, start=1):
        role = "tool" if isinstance(message, ToolResultMessage) else message.role
        lines.append(f"{index}. {role}: {_message_text(message)}")
    return "\n".join(lines)


def _message_text(message: AgentMessage) -> str:
    text = message_text(message)
    if isinstance(message, AssistantMessage) and message.tool_calls:
        names = ", ".join(call.name for call in message.tool_calls)
        text = f"{text} [tool calls: {names}]"
    elif isinstance(message, ToolResultMessage):
        status = "failed" if message.is_error else "ok"
        text = f"{message.tool_name} {status}: {text}"
    return _truncate_summary_text(text)


def _truncate_summary_text(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= SUMMARY_MESSAGE_CHAR_LIMIT:
        return collapsed
    return collapsed[: SUMMARY_MESSAGE_CHAR_LIMIT - 3].rstrip() + "..."
