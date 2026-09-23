"""The Core's context-budget guard: measure, never rewrite, refuse an oversized view.

`ContextBudgetGuard` is the last gate before physical I/O and knows nothing about
compaction: an extension owns every rewrite, so these tests pin that `freeze`
returns a detached-but-unchanged view, that the measurements are deterministic,
and that a view above the model window is refused with `ContextBudgetExceeded`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from run_agent_coding.context_budget import (
    ContextBudgetExceeded,
    ContextBudgetGuard,
    ProviderView,
)
from run_agent_core.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider import ModelRequest


def _request(messages: list[object]) -> ModelRequest:
    return ModelRequest("model", "system", messages, (), "session")  # type: ignore[arg-type]


def _guard(context_window_tokens: int = 10_000) -> ContextBudgetGuard:
    return ContextBudgetGuard(context_window_tokens=context_window_tokens)


def _tool_group(call_id: str, text: str) -> list[object]:
    assistant = AssistantMessage(
        content=[ToolCall(id=call_id, name="read", arguments={"path": "a.py"})],
        stop_reason="toolUse",
    )
    result = ToolResultMessage(
        tool_call_id=call_id,
        tool_name="read",
        content=[TextContent(text=text)],
    )
    return [assistant, result]


def _messages(view: ProviderView) -> list[object]:
    return list(view.request.messages)


def test_freeze_returns_a_detached_view_with_equal_token_counts(tmp_path: Path) -> None:
    """Nothing is rewritten, so the two measurements of a frozen view are equal."""
    message = UserMessage(content="small")
    request = _request([message])

    view = _guard().freeze(request)

    assert isinstance(view, ProviderView)
    assert view.tokens_before == view.tokens_after
    assert view.tokens_before > 0
    assert _messages(view)[0] == message
    assert _messages(view)[0] is not message, "the view must not alias the transcript"
    assert len(view.stable_prefix_digest) == 64


def test_freeze_rewrites_nothing_even_for_an_oversized_view(tmp_path: Path) -> None:
    """No spill, no folding, no result compaction: a big tool result stays verbatim."""
    payload = "x" * 5_000
    request = _request([UserMessage(content="start"), *_tool_group("call-1", payload)])

    view = _guard(context_window_tokens=100).freeze(request)

    assert view.tokens_after == view.tokens_before
    assert _messages(view)[2].text == payload  # type: ignore[union-attr]
    assert not (tmp_path / ".run" / "context" / "blobs").exists()


def test_hard_limit_blocks_physical_request() -> None:
    request = _request([UserMessage(content="x" * 50_000)])

    view = _guard(context_window_tokens=100).freeze(request)

    with pytest.raises(ContextBudgetExceeded, match="persistent compaction is required"):
        _guard(context_window_tokens=100).require_hard_limit(view)


def test_a_view_inside_the_window_passes_the_hard_limit() -> None:
    request = _request([UserMessage(content="small")])

    guard = _guard(context_window_tokens=10_000)

    guard.require_hard_limit(guard.freeze(request))


def test_the_window_must_be_positive() -> None:
    with pytest.raises(ValueError, match="Context window must be positive"):
        ContextBudgetGuard(context_window_tokens=0)


def test_stable_prefix_digest_ignores_an_appended_tail() -> None:
    """Only a new persisted summary prefix may move the digest, never a tail entry."""
    guard = _guard()

    first = guard.freeze(_request([UserMessage(content="root request")]))
    grown = guard.freeze(
        _request(
            [
                UserMessage(content="root request"),
                *_tool_group("call-1", "tool output"),
                AssistantMessage(content="answer"),
                UserMessage(content="follow-up"),
            ]
        )
    )

    assert grown.stable_prefix_digest == first.stable_prefix_digest


def test_stable_prefix_digest_changes_with_a_new_summary_prefix() -> None:
    """A new persisted summary rewrites the head, so the digest must change with it."""
    guard = _guard()
    prefix = "Previous conversation summary:\n"

    before = guard.freeze(_request([UserMessage(content="root request")]))
    first = guard.freeze(
        _request([UserMessage(content=f"{prefix}first summary"), UserMessage(content="tail")])
    )
    second = guard.freeze(
        _request([UserMessage(content=f"{prefix}second summary"), UserMessage(content="tail")])
    )

    assert first.stable_prefix_digest != before.stable_prefix_digest
    assert second.stable_prefix_digest != first.stable_prefix_digest
