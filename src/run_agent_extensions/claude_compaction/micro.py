"""L1 microcompact: tool-result level, no model call.

Ports ``microCompact.ts`` + ``cachedMicrocompact.ts`` + ``timeBasedMCConfig.ts``:

* the compactable tool set is ``Read/Bash/PowerShell/Grep/Glob/WebSearch/WebFetch/
  Edit/Write`` plus this project's own tool names for the same jobs;
* a cleared result keeps its message and its tool-call pairing and only has its
  content replaced with ``[Old tool result content cleared]``;
* ``keepRecent`` (5) most-recent compactable results always survive;
* the time-based trigger (``gapThresholdMinutes`` 60) fires when the gap since
  the last assistant message exceeds the threshold — the server cache is
  certainly cold then, so clearing is free;
* otherwise the count-based rule of the cached path applies: above
  ``TRIGGER_THRESHOLD`` (10) live results, delete the oldest
  ``active[:len - KEEP_RECENT]``.

``cachedMicrocompact`` deletes results through a ``cache_edits`` block so the
provider's cached prefix is preserved byte-for-byte. The extension API has no
cache-editing channel, so this port expresses the same intent locally: it never
touches the cacheable prefix (see :func:`protected_prefix_length`) and only
clears results outside it. The equivalence is documented in the package README.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.types import JSONValue

from .state import protected_prefix_length

TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"
IMAGE_MAX_TOKEN_SIZE = 2_000

# Reference tool names (FileRead, SHELL_TOOL_NAMES, Grep, Glob, WebSearch,
# WebFetch, FileEdit, FileWrite) mapped onto this project's built-ins.
SHELL_TOOL_NAMES = frozenset({"Bash", "PowerShell", "bash"})
COMPACTABLE_TOOL_NAMES: frozenset[str] = (
    frozenset(
        {
            "Read",
            "Edit",
            "Write",
            "Grep",
            "Glob",
            "WebSearch",
            "WebFetch",
            "read",
            "edit",
            "write",
            "grep",
            "find",
            "web_search",
            "web_fetch",
        }
    )
    | SHELL_TOOL_NAMES
)


def rough_token_count_estimation(text: str, bytes_per_token: int = 4) -> int:
    """Return ``round(len(text) / bytes_per_token)`` (the reference heuristic)."""
    return math.floor(len(text) / bytes_per_token + 0.5)


def _content_tokens(content: object) -> int:
    if isinstance(content, str):
        return rough_token_count_estimation(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if isinstance(block, TextContent):
            total += rough_token_count_estimation(block.text)
        elif isinstance(block, ImageContent):
            total += IMAGE_MAX_TOKEN_SIZE
    return total


def tool_result_tokens(message: ToolResultMessage) -> int:
    """Return the estimated tokens of one tool result's content."""
    return _content_tokens(message.content)


def estimate_message_tokens(messages: Sequence[AgentMessage]) -> int:
    """Estimate the tokens of a message sequence, padded by 4/3.

    Text and thinking count through :func:`rough_token_count_estimation`, a tool
    call counts ``name + input`` (not its JSON wrapper or id), an image counts
    ``IMAGE_MAX_TOKEN_SIZE``, and the total is padded
    ``ceil(total * 4 / 3)`` — all as in ``estimateMessageTokens``.
    """
    total = 0
    for message in messages:
        if isinstance(message, UserMessage):
            total += _content_tokens(message.content)
        elif isinstance(message, ToolResultMessage):
            total += tool_result_tokens(message)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextContent):
                    total += rough_token_count_estimation(block.text)
                elif isinstance(block, ThinkingContent):
                    if block.redacted:
                        total += rough_token_count_estimation(
                            json.dumps(block.model_dump(mode="json"))
                        )
                    else:
                        total += rough_token_count_estimation(block.thinking)
                else:
                    total += rough_token_count_estimation(
                        block.name + json.dumps(block.arguments, ensure_ascii=False)
                    )
        elif isinstance(message, BashExecutionMessage):
            total += rough_token_count_estimation(message.output)
        elif isinstance(message, CustomMessage):
            total += _content_tokens(message.content)
        elif isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
            total += rough_token_count_estimation(message.summary)
    return math.ceil(total * 4 / 3)


def estimate_request_tokens(system: str, messages: Sequence[AgentMessage]) -> int:
    """Estimate the whole request: system text plus the message sequence."""
    total = estimate_message_tokens(messages)
    return total + rough_token_count_estimation(system)


def compactable_tool_call_ids(messages: Sequence[AgentMessage]) -> list[str]:
    """Return compactable tool-call ids in encounter order."""
    ids: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCall) and block.name in COMPACTABLE_TOOL_NAMES:
                    ids.append(block.id)
    return ids


def result_index_by_call_id(messages: Sequence[AgentMessage]) -> dict[str, int]:
    """Map each tool-call id to the index of its result message."""
    indexes: dict[str, int] = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            indexes.setdefault(message.tool_call_id, index)
    return indexes


def get_tool_results_to_delete(
    active_ids: Sequence[str], *, trigger_threshold: int, keep_recent: int
) -> list[str]:
    """Return the oldest ids to delete once ``trigger_threshold`` is exceeded.

    A 1:1 port of ``getToolResultsToDelete``: no deletion at or below the
    threshold, and the most recent ``keep_recent`` results always survive.
    """
    if len(active_ids) <= trigger_threshold:
        return []
    return list(active_ids[: len(active_ids) - keep_recent])


def evaluate_time_based_trigger(
    messages: Sequence[AgentMessage],
    *,
    now_ms: float,
    gap_threshold_minutes: float,
) -> float | None:
    """Return the measured gap in minutes when the time-based trigger fires."""
    last_assistant = next(
        (message for message in reversed(messages) if isinstance(message, AssistantMessage)),
        None,
    )
    if last_assistant is None:
        return None
    gap_minutes = (now_ms - last_assistant.timestamp) / 60_000
    if not math.isfinite(gap_minutes) or gap_minutes < gap_threshold_minutes:
        return None
    return gap_minutes


@dataclass(frozen=True, slots=True)
class MicrocompactOutcome:
    """The rewritten view plus what L1 did to it."""

    messages: tuple[AgentMessage, ...]
    cleared_ids: tuple[str, ...] = ()
    tokens_saved: int = 0
    trigger: str | None = None
    gap_minutes: float | None = None

    @property
    def applied(self) -> bool:
        """Return whether any tool result content was cleared."""
        return bool(self.cleared_ids)


def microcompact(
    messages: Sequence[AgentMessage],
    *,
    keep_recent: int,
    cached_trigger_threshold: int,
    now_ms: float,
    time_based_enabled: bool = True,
    gap_threshold_minutes: float = 60,
) -> MicrocompactOutcome:
    """Run L1 over one request view.

    The time-based trigger runs first and short-circuits, exactly as in the
    reference: when it fires the cache is cold, so the count-based path is
    skipped. Neither path clears a result inside the cacheable prefix.
    """
    view = list(messages)
    protected = protected_prefix_length(view)
    compactable_ids = compactable_tool_call_ids(view)
    result_indexes = result_index_by_call_id(view)

    clear_ids: set[str] = set()
    trigger: str | None = None
    gap_minutes: float | None = None

    if time_based_enabled:
        gap_minutes = evaluate_time_based_trigger(
            view, now_ms=now_ms, gap_threshold_minutes=gap_threshold_minutes
        )
        if gap_minutes is not None:
            keep = max(1, keep_recent)
            keep_set = set(compactable_ids[-keep:])
            clear_ids = {call_id for call_id in compactable_ids if call_id not in keep_set}
            trigger = "time-based" if clear_ids else None

    if trigger is None:
        active = [
            call_id
            for call_id in compactable_ids
            if call_id in result_indexes
            and result_indexes[call_id] >= protected
            and _content_text(view[result_indexes[call_id]]) != TIME_BASED_MC_CLEARED_MESSAGE
        ]
        to_delete = get_tool_results_to_delete(
            active,
            trigger_threshold=cached_trigger_threshold,
            keep_recent=keep_recent,
        )
        if to_delete:
            clear_ids = set(to_delete)
            trigger = "count"

    if not clear_ids:
        return MicrocompactOutcome(messages=tuple(view))

    cleared: list[str] = []
    tokens_saved = 0
    rewritten: list[AgentMessage] = []
    for index, message in enumerate(view):
        if (
            isinstance(message, ToolResultMessage)
            and index >= protected
            and message.tool_call_id in clear_ids
            and _content_text(message) != TIME_BASED_MC_CLEARED_MESSAGE
        ):
            tokens_saved += tool_result_tokens(message)
            cleared.append(message.tool_call_id)
            rewritten.append(
                message.model_copy(
                    update={"content": [TextContent(text=TIME_BASED_MC_CLEARED_MESSAGE)]}
                )
            )
            continue
        rewritten.append(message)

    if not cleared:
        return MicrocompactOutcome(messages=tuple(view))
    return MicrocompactOutcome(
        messages=tuple(rewritten),
        cleared_ids=tuple(cleared),
        tokens_saved=tokens_saved,
        trigger=trigger,
        gap_minutes=gap_minutes,
    )


def _content_text(message: AgentMessage) -> str:
    if isinstance(message, ToolResultMessage):
        return message.text
    return ""


def compactable_summary(outcome: MicrocompactOutcome) -> Mapping[str, JSONValue]:
    """Return a JSON-safe diagnostic record for one L1 outcome."""
    return {
        "layer": "L1",
        "trigger": outcome.trigger,
        "cleared": len(outcome.cleared_ids),
        "tokensSaved": outcome.tokens_saved,
        "gapMinutes": None if outcome.gap_minutes is None else round(outcome.gap_minutes, 2),
    }


__all__ = [
    "COMPACTABLE_TOOL_NAMES",
    "IMAGE_MAX_TOKEN_SIZE",
    "SHELL_TOOL_NAMES",
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "MicrocompactOutcome",
    "compactable_summary",
    "compactable_tool_call_ids",
    "estimate_message_tokens",
    "estimate_request_tokens",
    "evaluate_time_based_trigger",
    "get_tool_results_to_delete",
    "microcompact",
    "result_index_by_call_id",
    "rough_token_count_estimation",
    "tool_result_tokens",
]
