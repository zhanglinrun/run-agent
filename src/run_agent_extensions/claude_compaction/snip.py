"""L2 snip: message level, no model call.

Ports ``snipCompact.ts`` + ``snipProjection.ts`` + ``src/commands/force-snip.ts``.

A snip boundary is recorded as a ``CustomEntry`` (``SNIP_NAMESPACE``) whose
``snipMetadata.removedUuids`` names the messages to drop — the same payload shape
as the reference's ``system``/``snip_boundary`` message, minus the message itself:
Run Agent's provider view accepts only user/assistant/toolResult roles, so the
boundary lives in the durable log and the view gets the boundary *text* as a user
message instead (see :func:`project_snipped_view`).

Two semantics are ported verbatim:

* the projection merges every boundary's removals and returns the *same list
  object* when there is nothing to remove (idempotent, no boundary → unchanged);
* ``snip_compact_if_needed`` honours only the last boundary and reports
  ``tokensFreed`` as ``sum(max(1, ceil(chars / 4)))``.

Removals are expanded to whole API rounds before they are applied, so a snip can
never leave an orphan tool result or an unresolved tool call.
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
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.types import JSONValue

from .state import SNIP_BOUNDARY_TEXT, SnipBoundary, message_key, protected_prefix_length

# From snipCompact.ts.
CHARS_PER_TOKEN = 4
SNIP_NUDGE_THRESHOLD = 30
SNIP_NUDGE_TEXT = (
    "The conversation history is getting long. Consider using the /force-snip command "
    "or the snip tool to compress older messages, freeing context window space for "
    "continued work."
)


def estimate_message_chars(message: AgentMessage) -> int:
    """Return the serialized character count of one message.

    Mirrors ``estimateMessageTokens`` in ``snipCompact.ts``: block text counts,
    a block without text counts as its JSON serialization, and a message with no
    block content falls back to its whole serialized payload.
    """
    if isinstance(message, (UserMessage, CustomMessage, ToolResultMessage)):
        return _content_chars(message.content)
    if isinstance(message, AssistantMessage):
        chars = 0
        for block in message.content:
            if isinstance(block, TextContent):
                chars += len(block.text)
            elif isinstance(block, ThinkingContent):
                chars += len(block.thinking)
            else:
                chars += len(json.dumps(block.model_dump(mode="json"), ensure_ascii=False))
        return chars
    if isinstance(message, BashExecutionMessage):
        return len(message.output)
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return len(message.summary)
    return len(json.dumps(message.model_dump(mode="json"), ensure_ascii=False))


def _content_chars(content: str | list[TextContent | ImageContent]) -> int:
    if isinstance(content, str):
        return len(content)
    chars = 0
    for block in content:
        if isinstance(block, TextContent):
            chars += len(block.text)
        else:
            chars += len(json.dumps(block.model_dump(mode="json"), ensure_ascii=False))
    return chars


def tokens_freed_for(messages: Sequence[AgentMessage], removed_keys: frozenset[str]) -> int:
    """Return ``sum(max(1, ceil(chars / 4)))`` over the removed messages."""
    freed = 0
    for message in messages:
        if message_key(message) in removed_keys:
            chars = estimate_message_chars(message)
            freed += max(1, math.ceil(chars / CHARS_PER_TOKEN))
    return freed


def boundary_removed_keys(boundaries: Sequence[SnipBoundary]) -> frozenset[str]:
    """Merge every boundary's removals into one key set."""
    removed: set[str] = set()
    for boundary in boundaries:
        removed.update(boundary.removed)
    return frozenset(removed)


def expand_to_api_rounds(
    messages: Sequence[AgentMessage], removed_keys: frozenset[str]
) -> frozenset[str]:
    """Grow a removal set to whole tool-call pairs.

    Removing part of a pair is what breaks tool pairing; the reference relies on
    its API-time ``ensureToolResultPairing`` to repair that, and this port refuses
    to create the situation in the first place. The unit is one assistant message
    plus every result that answers its calls — never a trailing user message, so a
    snip can never swallow the prompt the run is answering.
    """
    if not removed_keys:
        return removed_keys
    view = list(messages)
    expanded: set[str] = set(removed_keys)
    for message in view:
        if not isinstance(message, AssistantMessage):
            continue
        call_ids = {call.id for call in message.tool_calls}
        if not call_ids:
            continue
        group = {message_key(message)}
        for candidate in view:
            if isinstance(candidate, ToolResultMessage) and candidate.tool_call_id in call_ids:
                group.add(message_key(candidate))
        if group & removed_keys:
            expanded |= group
    return frozenset(expanded)


def project_snipped_view(
    messages: Sequence[AgentMessage],
    removed_keys: frozenset[str],
    *,
    marker_text: str = SNIP_BOUNDARY_TEXT,
    insert_marker: bool = True,
) -> list[AgentMessage]:
    """Return the model-facing view with snipped messages removed.

    With no removals the input list is returned unchanged (the reference returns
    the original array reference — this is the port's equivalent), which is what
    makes the projection idempotent. The cacheable prefix and the boundary text
    are always preserved.
    """
    view: list[AgentMessage] = messages if isinstance(messages, list) else list(messages)
    if not removed_keys:
        return view
    effective = expand_to_api_rounds(view, removed_keys)
    protected = protected_prefix_length(view)
    kept: list[AgentMessage] = []
    removed_count = 0
    for index, message in enumerate(view):
        if index < protected or message_key(message) not in effective:
            kept.append(message)
        else:
            removed_count += 1
    if removed_count == 0:
        return view
    if not insert_marker:
        return kept
    head = kept[:protected]
    tail = kept[protected:]
    return [*head, UserMessage(content=marker_text), *tail]


@dataclass(frozen=True, slots=True)
class SnipResult:
    """Result of the ``snipCompactIfNeeded`` semantics."""

    messages: list[AgentMessage]
    executed: bool
    tokens_freed: int


def snip_compact_if_needed(
    messages: Sequence[AgentMessage], boundaries: Sequence[SnipBoundary]
) -> SnipResult:
    """Apply only the last boundary, as ``snipCompactIfNeeded`` does."""
    last = boundaries[-1] if boundaries else None
    if last is None:
        return SnipResult(messages=list(messages), executed=False, tokens_freed=0)
    removed = frozenset(last.removed)
    if not removed:
        return SnipResult(messages=list(messages), executed=True, tokens_freed=0)
    return SnipResult(
        messages=project_snipped_view(messages, removed),
        executed=True,
        tokens_freed=tokens_freed_for(messages, removed),
    )


def should_nudge(messages: Sequence[AgentMessage], threshold: int = SNIP_NUDGE_THRESHOLD) -> bool:
    """Return whether the conversation is long enough to nudge for a snip."""
    return len(messages) >= threshold


def select_keys_for_request(
    messages: Sequence[AgentMessage],
    *,
    message_ids: Sequence[str] = (),
    range_start: int | None = None,
    range_end: int | None = None,
    keep_recent: int | None = None,
) -> tuple[str, ...]:
    """Resolve the ``snip`` tool's arguments into message keys.

    ``message_ids`` accepts this extension's keys (``cc:…``) or 1-based ordinals
    (``m3``/``3``) over the durable transcript; ``range_start``/``range_end`` are
    inclusive 1-based ordinals; ``keep_recent`` marks everything older than the
    last N messages. Unknown ids are ignored rather than failing the call.
    """
    keys: list[str] = []
    seen: set[str] = set()

    def add(message: AgentMessage) -> None:
        key = message_key(message)
        if key not in seen:
            seen.add(key)
            keys.append(key)

    if message_ids:
        known = {message_key(message): message for message in messages}
        for raw in message_ids:
            text = raw.strip()
            if text in known:
                add(known[text])
                continue
            ordinal = _parse_ordinal(text)
            if ordinal is not None and 1 <= ordinal <= len(messages):
                add(messages[ordinal - 1])
    if range_start is not None or range_end is not None:
        start = range_start if range_start is not None else 1
        end = range_end if range_end is not None else len(messages)
        for index in range(max(1, start), min(len(messages), end) + 1):
            add(messages[index - 1])
    if keep_recent is not None:
        keep = max(0, keep_recent)
        cutoff = max(0, len(messages) - keep)
        for message in messages[:cutoff]:
            add(message)
    return tuple(keys)


def select_all_keys(messages: Sequence[AgentMessage]) -> tuple[str, ...]:
    """Return every message key in ``messages`` (the ``/force-snip`` semantics)."""
    return tuple(message_key(message) for message in messages)


def snip_tool_result(count: int, reason: str | None) -> str:
    """Return the tool result text for one snip call."""
    summary = reason or f"Snipped {count} messages"
    return f"Snipped {count} messages. Summary: {summary}"


def snip_tool_schema() -> Mapping[str, JSONValue]:
    """Return the JSON schema of the ``snip`` tool's arguments."""
    return {
        "type": "object",
        "properties": {
            "message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Messages to snip, by transcript ordinal (m1, m2, …) or by the key "
                    "shown for a message. Snipped messages leave the model view."
                ),
            },
            "range_start": {
                "type": "integer",
                "description": "First transcript ordinal to snip (1-based, inclusive).",
            },
            "range_end": {
                "type": "integer",
                "description": "Last transcript ordinal to snip (1-based, inclusive).",
            },
            "keep_recent": {
                "type": "integer",
                "description": "Snip every message older than the last N messages.",
            },
            "reason": {
                "type": "string",
                "description": "Why these messages are being snipped, kept in the boundary.",
            },
        },
        "additionalProperties": False,
    }


def _parse_ordinal(text: str) -> int | None:
    candidate = text[1:] if text[:1] in {"m", "M"} else text
    if not candidate.isdigit():
        return None
    return int(candidate)


__all__ = [
    "CHARS_PER_TOKEN",
    "SNIP_NUDGE_TEXT",
    "SNIP_NUDGE_THRESHOLD",
    "SnipResult",
    "boundary_removed_keys",
    "estimate_message_chars",
    "expand_to_api_rounds",
    "project_snipped_view",
    "select_all_keys",
    "select_keys_for_request",
    "should_nudge",
    "snip_compact_if_needed",
    "snip_tool_result",
    "snip_tool_schema",
    "tokens_freed_for",
]
