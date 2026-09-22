from __future__ import annotations

from pathlib import Path

import pytest

from run_agent_coding.context_view import ContextBudgetExceeded, ContextViewPipeline
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


def test_large_results_are_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    big = "x" * 200
    messages = [UserMessage(content="start"), *_tool_group("../../escape", big)]
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=40,
        reserve_tokens=5,
        spill_chars=50,
        spill_preview_chars=20,
        keep_recent_tokens=10,
    )

    first = pipeline.prepare(_request(messages))
    second = pipeline.prepare(_request(messages))

    assert first.artifacts == second.artifacts
    assert len(first.artifacts) == 1
    artifact = first.artifacts[0]
    assert ".." not in artifact.relative_path
    blobs = list((tmp_path / ".run" / "context" / "blobs").glob("*.txt"))
    assert len(blobs) == 1
    assert blobs[0].read_text(encoding="utf-8") == big


def test_middle_fold_keeps_parallel_tool_groups_intact(tmp_path: Path) -> None:
    messages: list[object] = [UserMessage(content="root")]
    for index in range(6):
        messages.append(UserMessage(content=f"turn-{index} " + "q" * 300))
        messages.extend(_tool_group(f"call-{index}", "result " + "z" * 500))
        messages.append(AssistantMessage(content=f"done-{index}"))
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=500,
        reserve_tokens=100,
        spill_chars=10_000,
        keep_recent_tokens=100,
        compact_result_chars=50,
        keep_recent_results=1,
    )

    prepared = pipeline.prepare(_request(messages))
    view = list(prepared.request.messages)
    for index, message in enumerate(view):
        if isinstance(message, AssistantMessage) and message.tool_calls:
            following = view[index + 1 : index + 1 + len(message.tool_calls)]
            assert len(following) == len(message.tool_calls)
            assert all(isinstance(item, ToolResultMessage) for item in following)
            assert [item.tool_call_id for item in following] == [
                call.id for call in message.tool_calls
            ]
    assert "L1" in prepared.layers
    assert prepared.tokens_after < prepared.tokens_before


def test_below_target_is_a_detached_noop(tmp_path: Path) -> None:
    message = UserMessage(content="small")
    request = _request([message])
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=10_000,
        reserve_tokens=100,
    )
    prepared = pipeline.prepare(request)
    assert prepared.layers == ()
    assert prepared.needs_l4 is False
    assert prepared.request.messages[0] == message
    assert prepared.request.messages[0] is not message


def test_hard_limit_blocks_physical_request(tmp_path: Path) -> None:
    request = _request([UserMessage(content="x" * 50_000)])
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=100,
        reserve_tokens=10,
        keep_recent_tokens=1,
    )
    prepared = pipeline.prepare(request)
    assert prepared.needs_l4 is True
    with pytest.raises(ContextBudgetExceeded):
        pipeline.require_hard_limit(prepared)


def test_summary_only_reports_need_without_mutating(tmp_path: Path) -> None:
    request = _request([UserMessage(content="x" * 5000)])
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=100,
        reserve_tokens=10,
        strategy="summary-only",
    )
    prepared = pipeline.prepare(request)
    assert prepared.layers == ()
    assert prepared.needs_l4 is True
    assert prepared.request.messages[0].text == "x" * 5000  # type: ignore[union-attr]
