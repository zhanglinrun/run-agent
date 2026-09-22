"""L4 auto/compact, the reactive path and the failure circuit breaker.

Ports ``autoCompact.ts`` + ``compact.ts`` + ``reactiveCompact.ts``:

* window arithmetic — the effective window is ``context_window -
  min(getMaxOutputTokensForModel, MAX_OUTPUT_TOKENS_FOR_SUMMARY)`` with
  ``MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000``, the buffer grows with the window
  (13k/30k/50k), and ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` (here
  ``COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE``) can only lower the
  threshold;
* the split point comes from :func:`select_split_index` and is always snapped to an
  API round by ``grouping.aligned_start_index``, so the retained tail never
  splits a tool call from its results;
* the summary itself is one bounded ``inference.complete`` call (the host owns
  the provider), wrapped in ``asyncio.wait_for`` because the request has no
  timeout of its own, with the reference's prompt-too-long retry
  (``truncateHeadForPTLRetry``) when the host reports that error;
* the circuit breaker stops after ``MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`` (3)
  consecutive failures — the reference's BQ-driven fix for sessions that
  hammered the API with doomed compaction attempts;
* the reactive path is armed by an observed provider error (HTTP 413 or an
  assistant error message that names a context overflow or an oversized image),
  runs once per run regardless of the threshold switch, and on failure only logs.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceRequest,
    InferenceResult,
    InferenceService,
    InferenceUnavailable,
)
from run_agent_core.messages import AgentMessage

from .config import FourLayerConfig
from .grouping import MAX_PTL_RETRIES, aligned_start_index, truncate_head_for_retry
from .micro import estimate_message_tokens
from .prompt import get_compact_prompt

MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000
TOOL_RESULT_GROWTH_ESTIMATE = 15_000
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
DEFAULT_KEEP_RECENT_TOKENS = 20_000

SUMMARY_PURPOSE = "claude_compaction_summary"

# `is_context_overflow_error` (session.py) mirrored: the extension must not import
# the coding session module (it imports the extension runtime), so the marker list
# is duplicated deliberately and covered by tests.
_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context window",
    "context limit",
    "maximum context",
    "max context",
    "input is too long",
    "input length",
    "prompt is too long",
    "too many tokens",
    "token limit",
    "exceeds the limit",
    "exceeded the limit",
)

_MEDIA_MARKERS: tuple[str, ...] = (
    "image",
    "media",
    "attachment",
)


class SummaryUnavailable(RuntimeError):
    """Raised when a summary attempt produced no usable text."""


class SummaryDeferred(RuntimeError):
    """Raised when the host could not even attempt the summary (foreground run)."""


@dataclass(frozen=True, slots=True)
class TokenWarningState:
    """The reference's ``calculateTokenWarningState`` projection."""

    percent_left: int
    is_above_warning_threshold: bool
    is_above_error_threshold: bool
    is_above_auto_compact_threshold: bool
    is_at_blocking_limit: bool


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """One successful summarization: the text and the call's bookkeeping."""

    text: str
    model: str
    snapshot_id: str
    input_tokens: int
    output_tokens: int
    attempts: int = 1


def effective_context_window_size(context_window: int, max_output_tokens: int) -> int:
    """Return ``context_window - min(max_output_tokens, 20_000)``."""
    reserved = min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return context_window - reserved


def autocompact_buffer_tokens(effective_window: int) -> int:
    """Return the context-aware autocompact buffer for an effective window."""
    if effective_window >= 800_000:
        return 50_000
    if effective_window >= 400_000:
        return 30_000
    return AUTOCOMPACT_BUFFER_TOKENS


def auto_compact_threshold(config: FourLayerConfig) -> int:
    """Return the token count at which L4 must run."""
    effective = effective_context_window_size(
        config.context_window, config.max_output_tokens_for_summary
    )
    threshold = effective - autocompact_buffer_tokens(effective)
    if config.autocompact_pct_override is not None:
        percentage = math.floor(effective * (config.autocompact_pct_override / 100))
        return min(percentage, threshold)
    return threshold


def estimate_max_turn_growth(config: FourLayerConfig) -> int:
    """Estimate the largest growth one turn can add."""
    max_output = min(config.max_output_tokens_for_summary, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return max_output + TOOL_RESULT_GROWTH_ESTIMATE


def calculate_token_warning_state(token_usage: int, config: FourLayerConfig) -> TokenWarningState:
    """Project the reference's warning/error/autocompact/blocking thresholds."""
    effective = effective_context_window_size(
        config.context_window, config.max_output_tokens_for_summary
    )
    auto_threshold = auto_compact_threshold(config)
    threshold = auto_threshold if config.l4_enabled else effective
    percent_left = max(0, math.floor((threshold - token_usage) / threshold * 100 + 0.5))
    blocking_limit = effective - MANUAL_COMPACT_BUFFER_TOKENS
    return TokenWarningState(
        percent_left=percent_left,
        is_above_warning_threshold=token_usage >= threshold - WARNING_THRESHOLD_BUFFER_TOKENS,
        is_above_error_threshold=token_usage >= threshold - ERROR_THRESHOLD_BUFFER_TOKENS,
        is_above_auto_compact_threshold=config.l4_enabled and token_usage >= auto_threshold,
        is_at_blocking_limit=token_usage >= blocking_limit,
    )


def should_auto_compact(token_usage: int, config: FourLayerConfig) -> bool:
    """Return whether the proactive threshold is crossed and L4 is enabled."""
    if not config.enabled or not config.l4_enabled:
        return False
    return calculate_token_warning_state(token_usage, config).is_above_auto_compact_threshold


def select_split_index(
    messages: Sequence[AgentMessage], *, keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
) -> int:
    """Return the first index kept verbatim in an L4 view.

    Walks backwards until the retained tail reaches ``keep_recent_tokens``, then
    snaps to the enclosing API round. Always leaves at least one row to replace,
    so a requested summary can never name the head as its boundary.
    """
    if len(messages) < 2:
        return len(messages)
    index = len(messages)
    accumulated = 0
    for position in range(len(messages) - 1, -1, -1):
        accumulated += estimate_message_tokens([messages[position]])
        index = position
        if accumulated >= keep_recent_tokens:
            break
    aligned = aligned_start_index(messages, index)
    if aligned <= 0:
        return 1 if len(messages) > 1 else len(messages)
    return min(aligned, len(messages) - 1)


def is_context_overflow_error(text: str) -> bool:
    """Return whether an error message looks like a context overflow."""
    normalized = text.lower()
    return any(marker in normalized for marker in _OVERFLOW_MARKERS)


def is_media_size_error(text: str) -> bool:
    """Return whether an error message looks like an oversized image/attachment."""
    normalized = text.lower()
    if not any(marker in normalized for marker in _MEDIA_MARKERS):
        return False
    return any(
        marker in normalized
        for marker in ("too large", "exceed", "maximum size", "max size", "too big")
    )


def reactive_reason_for_status(status: int) -> str | None:
    """Classify an HTTP status for the reactive path.

    Only ``413`` is conclusive on its own (payload/media too large); a ``400``
    cannot be told apart from an unrelated bad request without the body, so it is
    reported as a suspicion and the ``message_end`` error text decides.
    """
    if status == 413:
        return "media_size"
    if status == 400:
        return "prompt_too_long_suspected"
    return None


def reactive_reason_for_error_text(text: str) -> str | None:
    """Classify one assistant error message for the reactive path."""
    if is_context_overflow_error(text):
        return "prompt_too_long"
    if is_media_size_error(text):
        return "media_size"
    return None


def render_messages_for_summary(
    messages: Sequence[AgentMessage], *, per_message_char_limit: int = 8_000
) -> str:
    """Render the messages to summarize as a bounded transcript."""
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        body = json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
        if len(body) > per_message_char_limit:
            body = body[:per_message_char_limit] + "…[truncated]"
        lines.append(f'<message index="{index}" role="{message.role}">{body}</message>')
    return "\n".join(lines)


def build_summary_prompt(
    messages: Sequence[AgentMessage], *, custom_instructions: str | None = None
) -> str:
    """Return the full summarization prompt for a message prefix."""
    return (
        f"{get_compact_prompt(custom_instructions)}\n\n"
        f"Conversation transcript to summarize:\n{render_messages_for_summary(messages)}"
    )


async def request_summary(
    inference: InferenceService,
    messages: Sequence[AgentMessage],
    *,
    config: FourLayerConfig,
    custom_instructions: str | None = None,
    purpose: str = SUMMARY_PURPOSE,
) -> SummaryResult:
    """Ask the host for one summary, bounded and retried on prompt-too-long.

    ``InferenceUnavailable`` / ``InferenceBusy`` propagate untouched (the caller
    decides whether that defers or counts as a failure); an empty answer, a
    timeout, or a repeated prompt-too-long raise :class:`SummaryUnavailable`.
    """
    attempt_messages = list(messages)
    attempts = 0
    for retry in range(MAX_PTL_RETRIES + 1):
        attempts += 1
        request = InferenceRequest(
            prompt=build_summary_prompt(attempt_messages, custom_instructions=custom_instructions),
            system="",
            purpose=purpose,
            max_output_tokens=config.max_output_tokens_for_summary,
        )
        try:
            result: InferenceResult = await asyncio.wait_for(
                inference.complete(request), timeout=config.summary_timeout_seconds
            )
        except (InferenceBusy, InferenceUnavailable):
            raise
        except TimeoutError as exc:
            raise SummaryUnavailable("the summary request timed out") from exc
        except RuntimeError as exc:
            if retry < MAX_PTL_RETRIES and is_context_overflow_error(str(exc)):
                truncated = truncate_head_for_retry(attempt_messages)
                if not truncated:
                    raise SummaryUnavailable(str(exc)) from exc
                attempt_messages = truncated
                continue
            raise SummaryUnavailable(str(exc)) from exc
        text = result.text.strip()
        if not text:
            raise SummaryUnavailable("the summary request returned no text")
        return SummaryResult(
            text=text,
            model=result.model,
            snapshot_id=result.snapshot_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            attempts=attempts,
        )
    raise SummaryUnavailable("the summary request kept hitting prompt-too-long")


def summary_prompt_chars(messages: Sequence[AgentMessage]) -> int:
    """Return the character count of the rendered summary transcript."""
    return len(render_messages_for_summary(messages))


__all__ = [
    "AUTOCOMPACT_BUFFER_TOKENS",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "ERROR_THRESHOLD_BUFFER_TOKENS",
    "MANUAL_COMPACT_BUFFER_TOKENS",
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES",
    "MAX_OUTPUT_TOKENS_FOR_SUMMARY",
    "SUMMARY_PURPOSE",
    "TOOL_RESULT_GROWTH_ESTIMATE",
    "WARNING_THRESHOLD_BUFFER_TOKENS",
    "SummaryDeferred",
    "SummaryResult",
    "SummaryUnavailable",
    "TokenWarningState",
    "auto_compact_threshold",
    "autocompact_buffer_tokens",
    "build_summary_prompt",
    "calculate_token_warning_state",
    "effective_context_window_size",
    "estimate_max_turn_growth",
    "is_context_overflow_error",
    "is_media_size_error",
    "reactive_reason_for_error_text",
    "reactive_reason_for_status",
    "render_messages_for_summary",
    "request_summary",
    "select_split_index",
    "should_auto_compact",
    "summary_prompt_chars",
]
