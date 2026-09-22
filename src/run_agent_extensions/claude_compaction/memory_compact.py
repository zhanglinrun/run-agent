"""L3 session-memory compaction: session level, no model call.

Ports ``sessionMemoryCompact.ts`` with this project's memory files: the summary
is the *existing* ``MEMORY.md`` / ``USER.md`` content (user scope ``~/.run``,
project scope ``<cwd>/.run``), wrapped in the same continuation header the model
path uses. No inference request is made, which is what makes this layer the cheap
alternative to L4.

The memory files are read by path only. This package deliberately does not import
``run_agent_extensions.hermes_memory`` (see the README): the memory extension owns
writes and threat scanning, this port only reads the two documented files and
never writes to them.

Failure is the fall-back signal: an empty or missing memory file, a
``lastSummarizedMessageId`` that is no longer present, or a post-compaction
estimate that still crosses the auto threshold all return ``None`` so the caller
runs L4 instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths
from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

from .grouping import aligned_start_index, assistant_round_id
from .micro import estimate_message_tokens
from .prompt import get_compact_user_summary_message
from .state import message_key

MEMORY_FILE_NAMES: tuple[str, ...] = ("MEMORY.md", "USER.md")


@dataclass(frozen=True, slots=True)
class SMCompactConfig:
    """Thresholds for how much history L3 keeps verbatim."""

    min_tokens: int = 10_000
    min_text_block_messages: int = 5
    max_tokens: int = 40_000

    def __post_init__(self) -> None:
        if self.min_tokens < 0 or self.max_tokens < self.min_tokens:
            raise ValueError("Session-memory bounds must satisfy 0 <= min <= max")
        if self.min_text_block_messages < 0:
            raise ValueError("The text-block minimum must not be negative")


DEFAULT_SM_COMPACT_CONFIG = SMCompactConfig()


def memory_file_paths(paths: RunAgentPaths, cwd: Path) -> tuple[Path, ...]:
    """Return the four memory files L3 reads, user scope first."""
    user_dir = paths.home
    project_dir = paths.project_run_agent_dir(cwd)
    return tuple(user_dir / name for name in MEMORY_FILE_NAMES) + tuple(
        project_dir / name for name in MEMORY_FILE_NAMES
    )


@dataclass(frozen=True, slots=True)
class MemoryText:
    """The non-empty memory content L3 can summarize, and where it came from."""

    text: str
    sources: tuple[Path, ...]

    @property
    def empty(self) -> bool:
        """Return whether no memory file contributed content."""
        return not self.text.strip()


def read_memory_text(paths: RunAgentPaths, cwd: Path) -> MemoryText:
    """Read every non-empty memory file, headed by its path."""
    sections: list[str] = []
    sources: list[Path] = []
    for path in memory_file_paths(paths, cwd):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content.strip():
            continue
        sections.append(f"## {path.name} ({path})\n\n{content.strip()}")
        sources.append(path)
    return MemoryText(text="\n\n".join(sections), sources=tuple(sources))


def has_text_blocks(message: AgentMessage) -> bool:
    """Return whether a message carries text a reader would call conversation."""
    if isinstance(message, AssistantMessage):
        return any(isinstance(block, TextContent) for block in message.content)
    if isinstance(message, ToolResultMessage):
        return False
    if isinstance(message, (UserMessage, CustomMessage)):
        content = message.content
        if isinstance(content, str):
            return len(content) > 0
        return any(isinstance(block, TextContent) for block in content)
    if isinstance(message, BashExecutionMessage):
        return bool(message.output)
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return bool(message.summary)
    return False


def tool_result_call_ids(message: AgentMessage) -> tuple[str, ...]:
    """Return the tool-call ids a message answers (its own results, in this port)."""
    if isinstance(message, ToolResultMessage):
        return (message.tool_call_id,)
    return ()


def _message_round_id(message: AgentMessage) -> str | None:
    """Reuse the grouping rule so both modules agree on what one round is."""
    return assistant_round_id(message)


def adjust_index_to_preserve_api_invariants(
    messages: Sequence[AgentMessage], start_index: int
) -> int:
    """Move a start index back so it never splits a tool call from its results.

    Ports ``adjustIndexToPreserveAPIInvariants``: every result in the kept range
    pulls its call message in, and assistant chunks sharing a response id stay
    together.
    """
    if start_index <= 0 or start_index >= len(messages):
        return start_index
    adjusted = start_index
    needed = {
        call_id for message in messages[start_index:] for call_id in tool_result_call_ids(message)
    }
    if needed:
        for index in range(adjusted - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, ToolCall) and block.id in needed:
                    adjusted = index
                    needed.discard(block.id)
            if not needed:
                break
    round_ids = {
        round_id
        for message in messages[adjusted:]
        if (round_id := _message_round_id(message)) is not None
    }
    for index in range(adjusted - 1, -1, -1):
        round_id = _message_round_id(messages[index])
        if round_id is not None and round_id in round_ids:
            adjusted = index
    return adjusted


def calculate_messages_to_keep_index(
    messages: Sequence[AgentMessage],
    last_summarized_index: int,
    config: SMCompactConfig = DEFAULT_SM_COMPACT_CONFIG,
) -> int:
    """Return the first index kept verbatim after an L3 compaction.

    Ports ``calculateMessagesToKeepIndex``: start after the last summarized
    message, expand backwards until both minimums hold, and stop at the maximum.
    """
    if not messages:
        return 0
    start_index = last_summarized_index + 1 if last_summarized_index >= 0 else len(messages)
    total_tokens = estimate_message_tokens(messages[start_index:])
    text_messages = sum(1 for message in messages[start_index:] if has_text_blocks(message))

    def satisfied() -> bool:
        return total_tokens >= config.min_tokens and text_messages >= config.min_text_block_messages

    if total_tokens >= config.max_tokens or satisfied():
        return adjust_index_to_preserve_api_invariants(messages, start_index)

    for index in range(start_index - 1, -1, -1):
        message = messages[index]
        total_tokens += estimate_message_tokens([message])
        if has_text_blocks(message):
            text_messages += 1
        start_index = index
        if total_tokens >= config.max_tokens or satisfied():
            break
    return adjust_index_to_preserve_api_invariants(messages, start_index)


@dataclass(frozen=True, slots=True)
class MemoryCompactionPlan:
    """One L3 compaction: the summary, the boundary and what it replaces."""

    summary: str
    keep_index: int
    replaced_rows: int
    sources: tuple[Path, ...]
    tokens_before: int


def resolve_last_summarized_index(messages: Sequence[AgentMessage], key: str | None) -> int:
    """Return the index of the last summarized message, or -1 when unknown.

    ``key is None`` means a resumed session: session memory exists but the
    boundary is unknown, which the reference models as ``len(messages) - 1``.
    A key that is no longer present returns ``-1`` so the caller falls back to L4.
    """
    if key is None:
        return len(messages) - 1
    for index, message in enumerate(messages):
        if message_key(message) == key:
            return index
    return -1


def plan_memory_compaction(
    messages: Sequence[AgentMessage],
    *,
    memory: MemoryText,
    last_summarized_key: str | None,
    transcript_path: str | None = None,
    config: SMCompactConfig = DEFAULT_SM_COMPACT_CONFIG,
    threshold: int | None = None,
) -> MemoryCompactionPlan | None:
    """Build an L3 plan, or ``None`` so the caller falls back to L4."""
    if memory.empty:
        return None
    last_index = resolve_last_summarized_index(messages, last_summarized_key)
    if last_index == -1:
        return None
    keep_index = calculate_messages_to_keep_index(messages, last_index, config)
    keep_index = aligned_start_index(messages, keep_index) if keep_index else 0
    if keep_index <= 0:
        return None
    kept = list(messages[keep_index:])
    summary = build_memory_summary(memory.text, transcript_path)
    if threshold is not None:
        post_compact = estimate_message_tokens([*kept, UserMessage(content=summary)])
        if post_compact >= threshold:
            return None
    return MemoryCompactionPlan(
        summary=summary,
        keep_index=keep_index,
        replaced_rows=keep_index,
        sources=memory.sources,
        tokens_before=estimate_message_tokens(messages),
    )


def build_memory_summary(memory_text: str, transcript_path: str | None) -> str:
    """Wrap existing memory content in the standard continuation header."""
    return get_compact_user_summary_message(
        memory_text,
        suppress_follow_up_questions=True,
        transcript_path=transcript_path,
        recent_messages_preserved=True,
    )


def memory_compaction_diagnostic(plan: MemoryCompactionPlan) -> Mapping[str, object]:
    """Return a JSON-safe diagnostic record for one L3 plan."""
    return {
        "layer": "L3",
        "keepIndex": plan.keep_index,
        "replacedRows": plan.replaced_rows,
        "tokensBefore": plan.tokens_before,
        "sources": [str(path) for path in plan.sources],
    }


__all__ = [
    "DEFAULT_SM_COMPACT_CONFIG",
    "MEMORY_FILE_NAMES",
    "SMCompactConfig",
    "MemoryCompactionPlan",
    "MemoryText",
    "adjust_index_to_preserve_api_invariants",
    "build_memory_summary",
    "calculate_messages_to_keep_index",
    "has_text_blocks",
    "memory_compaction_diagnostic",
    "memory_file_paths",
    "plan_memory_compaction",
    "read_memory_text",
    "resolve_last_summarized_index",
]
