"""API-round grouping, used by the summarizer's prompt-too-long retry.

A new group starts when a new assistant response begins, which is the one split
point the provider contract blesses (every tool call is resolved before the next
assistant turn). The reference gates on ``message.id``; this port gates on
``response_id``, and falls back to a per-message identity built from the model,
timestamp and content when a provider reported none — so two streamed chunks
that share a response id stay in one group while two distinct responses never
do.

:func:`truncate_head_for_retry` is the only consumer: when the summarizer's own
request overflows, whole groups are dropped from the oldest end until it fits
again, and never a partial round.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from hashlib import sha256

from run_agent_core.messages import AgentMessage, AssistantMessage, UserMessage

from .layers import estimate_message_tokens

# From the summarizer's own overflow retry.
MAX_PTL_RETRIES = 3
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"

def assistant_round_id(message: AgentMessage) -> str | None:
    """Return the API-round identity of an assistant message, if it has one."""
    if not isinstance(message, AssistantMessage):
        return None
    if message.response_id:
        return message.response_id
    body = json.dumps(message.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return f"{message.model}@{message.timestamp}#{sha256(body.encode('utf-8')).hexdigest()[:8]}"


def group_messages_by_api_round(
    messages: Sequence[AgentMessage],
) -> list[list[AgentMessage]]:
    """Group messages at API-round boundaries: one group per round trip."""
    groups: list[list[AgentMessage]] = []
    current: list[AgentMessage] = []
    last_assistant_id: str | None = None
    for message in messages:
        round_id = assistant_round_id(message)
        if round_id is not None and round_id != last_assistant_id and current:
            groups.append(current)
            current = [message]
        else:
            current.append(message)
        if round_id is not None:
            last_assistant_id = round_id
    if current:
        groups.append(current)
    return groups


def truncate_head_for_retry(
    messages: Sequence[AgentMessage],
    *,
    token_gap: int | None = None,
    drop_fraction: float = 0.2,
) -> list[AgentMessage]:
    """Drop the oldest API-round groups so a summarizer request fits again.

    Ports ``truncateHeadForPTLRetry``: drop whole groups (by token gap when the
    provider reported one, else 20% of them), keep at least one group, and
    re-assert a leading user message so the retried request stays provider-legal.
    """
    input_messages = list(messages)
    if (
        input_messages
        and isinstance(input_messages[0], UserMessage)
        and input_messages[0].text == PTL_RETRY_MARKER
    ):
        input_messages = input_messages[1:]
    groups = group_messages_by_api_round(input_messages)
    if len(groups) < 2:
        return []
    if token_gap is not None and token_gap > 0:
        accumulated = 0
        drop_count = 1
        for index, group in enumerate(groups, start=1):
            accumulated += estimate_message_tokens(group)
            drop_count = index
            if accumulated >= token_gap:
                break
    else:
        drop_count = max(1, math.floor(len(groups) * drop_fraction))
    drop_count = min(drop_count, len(groups) - 1)
    if drop_count < 1:
        return []
    sliced = [message for group in groups[drop_count:] for message in group]
    if sliced and isinstance(sliced[0], AssistantMessage):
        # Dropping group 0 leaves an assistant-first sequence, which providers
        # reject; the legalizer pairs the results this exposes.
        return [UserMessage(content=PTL_RETRY_MARKER), *sliced]
    return sliced


__all__ = [
    "MAX_PTL_RETRIES",
    "PTL_RETRY_MARKER",
    "assistant_round_id",
    "group_messages_by_api_round",
    "truncate_head_for_retry",
]
