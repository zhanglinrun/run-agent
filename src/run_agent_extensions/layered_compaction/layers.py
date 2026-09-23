# ruff: noqa: E501
"""The three free layers of the cheap-first pipeline: L1 → L2 → L3.

Ported from the reference implementation's ``context.py`` (its semantic numbering
is ``L3`` big-result persistence, ``L1`` middle snipping, ``L2`` old-result
placeholders; this port numbers the layers by execution order, see the package
README):

``L1`` :func:`persist_oversized_results`
    a tool result whose text exceeds the character threshold is written to
    ``cwd/.run/tool-results/<tool_call_id>.txt`` with an atomic replace, and the
    view keeps only a short preview that names the file. There is no automatic
    read-back: the model reads the file itself when it needs the full text.
``L2`` :func:`snip_middle`
    a view longer than the message limit keeps its first three and last
    ``limit - 4`` messages plus one user placeholder for the middle. Both cut
    points retreat to a pair boundary, so an ``assistant(tool calls) + results``
    group is never cut in half.
``L3`` :func:`placeholder_old_results`
    every tool result except the most recent ``keep_recent`` ones, whose text is
    longer than the character floor, has its content replaced with a short
    placeholder. ``tool_call_id`` and the rest of the message are untouched, so
    the tool call pairing survives.

Every function is a pure view transform: the input sequence is never mutated and
a layer that has nothing to do returns the view unchanged.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

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

# From the reference `budget_tool_results`.
PERSISTED_OUTPUT_MARKER = "<persisted-output>"
PERSISTED_OUTPUT_TEMPLATE = (
    "<persisted-output>\nFull: {path}\nPreview:\n{preview}\n</persisted-output>"
)
# From the reference `snip_messages`.
SNIPPED_MIDDLE_TEMPLATE = "[snipped {count} messages from conversation middle]"
# From the reference `micro_compact`.
EARLIER_TOOL_RESULT_PLACEHOLDER = "[Earlier tool result compacted]"

DEFAULT_PERSIST_THRESHOLD_CHARS = 20_000
DEFAULT_PERSIST_PREVIEW_CHARS = 2_000
DEFAULT_SNIP_MAX_MESSAGES = 50
DEFAULT_SNIP_HEAD_MESSAGES = 3
DEFAULT_PLACEHOLDER_MIN_CHARS = 200
DEFAULT_KEEP_RECENT_RESULTS = 5
CHARS_PER_TOKEN = 4
IMAGE_MAX_TOKEN_SIZE = 2_000

# The reference passes 3 here; the placeholder itself is the fourth message, so
# the tail is `max_messages - head_messages - 1` messages long.
_SNIP_FIXED_MESSAGES = 1


def rough_token_count_estimation(text: str, bytes_per_token: int = CHARS_PER_TOKEN) -> int:
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
    ``IMAGE_MAX_TOKEN_SIZE``, and the total is padded ``ceil(total * 4 / 3)``.
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
    return estimate_message_tokens(messages) + rough_token_count_estimation(system)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a same-directory temp file + replace.

    ``os.replace`` is atomic on both platforms the project supports, so a
    concurrent reader sees either the previous file or the complete new one,
    never a partial write. Re-running the same layer overwrites the same file
    idempotently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            with suppress(OSError):
                os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise


@dataclass(frozen=True, slots=True)
class PersistOutcome:
    """Result of L1: the rewritten view plus what the disk write did."""

    messages: tuple[AgentMessage, ...]
    persisted_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    chars_persisted: int = 0

    @property
    def applied(self) -> bool:
        """Return whether at least one oversized result left the view."""
        return bool(self.persisted_ids)


def _result_file_name(message: ToolResultMessage, index: int) -> str:
    """Return the file name of one persisted result (its tool-call id)."""
    return f"{message.tool_call_id.strip() or f'row-{index}'}.txt"


def persist_oversized_results(
    messages: Sequence[AgentMessage],
    *,
    results_dir: Path | None,
    threshold_chars: int = DEFAULT_PERSIST_THRESHOLD_CHARS,
    preview_chars: int = DEFAULT_PERSIST_PREVIEW_CHARS,
) -> PersistOutcome:
    """L1: persist oversized tool results and keep only a preview in the view.

    A missing directory, an unwritable one, or any other OS error degrades to
    keeping the original content: persisting is an optimisation, never a reason
    to lose a tool result. A message whose text is already the persisted-output
    marker is skipped, so a second pass over the same view is a no-op.
    """
    view = list(messages)
    if results_dir is None:
        return PersistOutcome(messages=tuple(view))

    persisted: list[str] = []
    failed: list[str] = []
    chars = 0
    for index, message in enumerate(view):
        if not isinstance(message, ToolResultMessage):
            continue
        text = message.text
        if len(text) <= threshold_chars or text.startswith(PERSISTED_OUTPUT_MARKER):
            continue
        path = Path(results_dir) / _result_file_name(message, index)
        try:
            atomic_write_text(path, text)
        except OSError:
            failed.append(message.tool_call_id)
            continue
        view[index] = message.model_copy(
            update={
                "content": [
                    TextContent(
                        text=PERSISTED_OUTPUT_TEMPLATE.format(
                            path=path, preview=text[:preview_chars]
                        )
                    )
                ]
            }
        )
        persisted.append(message.tool_call_id)
        chars += len(text)
    return PersistOutcome(
        messages=tuple(view),
        persisted_ids=tuple(persisted),
        failed_ids=tuple(failed),
        chars_persisted=chars,
    )


def has_tool_calls(message: AgentMessage) -> bool:
    return isinstance(message, AssistantMessage) and bool(message.tool_calls)


def snap_to_pair_boundary(messages: Sequence[AgentMessage], cut: int) -> int:
    """Retreat ``cut`` to a boundary that never splits a tool call from its results.

    The unit is one assistant message plus every result answering its calls: a
    cut may only move earlier, and it stops as soon as the message at the cut is
    not a tool result and the message before it carries no tool calls. A cut at
    or past the end of the view is returned unchanged: there is no pair to keep
    whole in a tail that does not exist.
    """
    if cut > len(messages):
        cut = len(messages)
    while (
        cut > 0
        and cut < len(messages)
        and (
            isinstance(messages[cut], ToolResultMessage)
            or has_tool_calls(messages[cut - 1])
        )
    ):
        cut -= 1
    return cut

@dataclass(frozen=True, slots=True)
class SnipOutcome:
    """Result of L2: the rewritten view, the placeholder and the cut points."""

    messages: tuple[AgentMessage, ...]
    snipped_count: int = 0
    head_end: int = 0
    tail_start: int = 0

    @property
    def applied(self) -> bool:
        """Return whether the middle of the view was removed."""
        return self.snipped_count > 0


def snip_middle(
    messages: Sequence[AgentMessage],
    *,
    max_messages: int = DEFAULT_SNIP_MAX_MESSAGES,
    head_messages: int = DEFAULT_SNIP_HEAD_MESSAGES,
) -> SnipOutcome:
    """L2: keep the head and the tail of a long view, replace the middle.

    A view at or below ``max_messages`` is returned unchanged. Otherwise the
    placeholder counts against the budget, so the tail is ``max_messages -
    head_messages - 1`` messages long, and both cut points retreat to a pair
    boundary. When the two cut points meet, cutting would split a round, so the
    view is returned unchanged (correctness over budget).
    """
    view = list(messages)
    if len(view) <= max_messages:
        return SnipOutcome(messages=tuple(view))
    keep_tail = max(0, max_messages - head_messages - _SNIP_FIXED_MESSAGES)
    head_end = snap_to_pair_boundary(view, min(head_messages, len(view)))
    tail_start = snap_to_pair_boundary(view, max(0, len(view) - keep_tail))
    if head_end >= tail_start:
        return SnipOutcome(messages=tuple(view))
    snipped = tail_start - head_end
    placeholder = UserMessage(content=SNIPPED_MIDDLE_TEMPLATE.format(count=snipped))
    return SnipOutcome(
        messages=tuple([*view[:head_end], placeholder, *view[tail_start:]]),
        snipped_count=snipped,
        head_end=head_end,
        tail_start=tail_start,
    )


@dataclass(frozen=True, slots=True)
class PlaceholderOutcome:
    """Result of L3: the rewritten view plus the ids whose content was replaced."""

    messages: tuple[AgentMessage, ...]
    replaced_ids: tuple[str, ...] = ()
    chars_saved: int = 0

    @property
    def applied(self) -> bool:
        """Return whether at least one old result was replaced."""
        return bool(self.replaced_ids)


def placeholder_old_results(
    messages: Sequence[AgentMessage],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT_RESULTS,
    min_chars: int = DEFAULT_PLACEHOLDER_MIN_CHARS,
) -> PlaceholderOutcome:
    """L3: replace the content of old, large tool results with a placeholder.

    The most recent ``keep_recent`` tool results always survive, a result whose
    text is at or below ``min_chars`` is left alone, and everything else keeps
    its identity: only ``content`` is replaced, so ``tool_call_id``, ``details``
    and ``is_error`` still describe the original call.

    A ``<persisted-output>`` preview is never replaced. The reference has no such
    exemption (its placeholder pass runs after its disk pass and would overwrite
    the preview), but then L1's one contract — the model can read the full result
    back from the named file — is lost as soon as the result is not among the
    most recent few. This is a deliberate deviation, noted in the package README.
    """
    view = list(messages)
    tool_indexes = [
        index for index, message in enumerate(view) if isinstance(message, ToolResultMessage)
    ]
    keep = max(0, keep_recent)
    # Fewer results than `keep_recent` means nothing is old enough to replace.
    # (The reference's raw slice would wrap around and replace the earliest ones
    # here; the rule it states is "all but the most recent five".)
    targets = tool_indexes[: max(0, len(tool_indexes) - keep)]
    replaced: list[str] = []
    chars_saved = 0
    for index in targets:
        message = view[index]
        assert isinstance(message, ToolResultMessage)  # noqa: S101 - index source
        text = message.text
        if len(text) <= min_chars or text.startswith(PERSISTED_OUTPUT_MARKER):
            continue
        view[index] = message.model_copy(
            update={"content": [TextContent(text=EARLIER_TOOL_RESULT_PLACEHOLDER)]}
        )
        replaced.append(message.tool_call_id)
        chars_saved += len(text) - len(EARLIER_TOOL_RESULT_PLACEHOLDER)
    return PlaceholderOutcome(
        messages=tuple(view),
        replaced_ids=tuple(replaced),
        chars_saved=chars_saved,
    )


@dataclass(frozen=True, slots=True)
class FreeLayerOutcome:
    """What the three free layers did to one request view."""

    messages: list[AgentMessage]
    changed: bool = False
    notes: list[str] = field(default_factory=list)


def run_free_layers(
    messages: Sequence[AgentMessage],
    *,
    results_dir: Path | None,
    persist_threshold_chars: int = DEFAULT_PERSIST_THRESHOLD_CHARS,
    persist_preview_chars: int = DEFAULT_PERSIST_PREVIEW_CHARS,
    snip_max_messages: int = DEFAULT_SNIP_MAX_MESSAGES,
    keep_recent_results: int = DEFAULT_KEEP_RECENT_RESULTS,
    placeholder_min_chars: int = DEFAULT_PLACEHOLDER_MIN_CHARS,
    enabled: tuple[bool, bool, bool] = (True, True, True),
) -> FreeLayerOutcome:
    """Run the whole free batch in order: L1 → L2 → L3.

    The order is the reference's: persisting first shrinks the biggest results,
    snipping second removes whole middle rounds, and the placeholder pass last
    sees the view the earlier two produced. Every layer is skipped when its
    switch is off.
    """
    view = list(messages)
    changed = False
    notes: list[str] = []

    if enabled[0]:
        persisted = persist_oversized_results(
            view,
            results_dir=results_dir,
            threshold_chars=persist_threshold_chars,
            preview_chars=persist_preview_chars,
        )
        if persisted.applied:
            view = list(persisted.messages)
            changed = True
        notes.append(
            f"L1 persisted {len(persisted.persisted_ids)} oversized tool result(s) "
            f"({persisted.chars_persisted} chars)"
            + (f"; {len(persisted.failed_ids)} write(s) failed" if persisted.failed_ids else "")
        )

    if enabled[1]:
        snipped = snip_middle(view, max_messages=snip_max_messages)
        if snipped.applied:
            view = list(snipped.messages)
            changed = True
            notes.append(
                f"L2 snipped {snipped.snipped_count} middle message(s) "
                f"(head {snipped.head_end}, tail from {snipped.tail_start})"
            )

    if enabled[2]:
        placeholders = placeholder_old_results(
            view, keep_recent=keep_recent_results, min_chars=placeholder_min_chars
        )
        if placeholders.applied:
            view = list(placeholders.messages)
            changed = True
            notes.append(
                f"L3 replaced {len(placeholders.replaced_ids)} old tool result(s) "
                f"({placeholders.chars_saved} chars saved)"
            )

    return FreeLayerOutcome(messages=view, changed=changed, notes=notes)


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_KEEP_RECENT_RESULTS",
    "DEFAULT_PERSIST_PREVIEW_CHARS",
    "DEFAULT_PERSIST_THRESHOLD_CHARS",
    "DEFAULT_PLACEHOLDER_MIN_CHARS",
    "DEFAULT_SNIP_HEAD_MESSAGES",
    "DEFAULT_SNIP_MAX_MESSAGES",
    "EARLIER_TOOL_RESULT_PLACEHOLDER",
    "IMAGE_MAX_TOKEN_SIZE",
    "PERSISTED_OUTPUT_MARKER",
    "PERSISTED_OUTPUT_TEMPLATE",
    "SNIPPED_MIDDLE_TEMPLATE",
    "FreeLayerOutcome",
    "PersistOutcome",
    "PlaceholderOutcome",
    "SnipOutcome",
    "atomic_write_text",
    "estimate_message_tokens",
    "estimate_request_tokens",
    "has_tool_calls",
    "persist_oversized_results",
    "placeholder_old_results",
    "rough_token_count_estimation",
    "run_free_layers",
    "snap_to_pair_boundary",
    "snip_middle",
    "tool_result_tokens",
]
