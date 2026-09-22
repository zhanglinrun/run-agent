from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_view import (
    ContextBlobDiagnostics,
    ContextBudgetExceeded,
    ContextViewPipeline,
    _write_blob_once,
    context_blob_diagnostics,
)
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider import ModelRequest
from run_agent_core.provider_events import AssistantDoneEvent


async def _drain(events) -> list[object]:
    return [event async for event in events]


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


def test_four_layer_prepares_nothing_and_still_refuses_an_oversized_view(tmp_path: Path) -> None:
    """`four-layer` hands L1-L4 to an extension but keeps the hard window guard."""
    payload = "x" * 5000
    request = _request([UserMessage(content="start"), *_tool_group("call-1", payload)])
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=100,
        reserve_tokens=10,
        strategy="four-layer",
        spill_chars=50,
        keep_recent_tokens=10,
    )

    prepared = pipeline.prepare(request)

    assert prepared.layers == (), "the core must not apply L3/L1/L2 under four-layer"
    assert prepared.artifacts == (), "no blob is written for a view nobody rewrote"
    assert prepared.tokens_after == prepared.tokens_before
    assert prepared.needs_l4 is True
    assert prepared.request.messages[2].text == payload  # type: ignore[union-attr]
    assert not (tmp_path / ".run" / "context" / "blobs").exists()
    with pytest.raises(ContextBudgetExceeded):
        pipeline.require_hard_limit(prepared)


def test_an_unknown_context_strategy_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown context strategy"):
        ContextViewPipeline(
            cwd=tmp_path,
            context_window_tokens=100,
            reserve_tokens=10,
            strategy="magic",  # type: ignore[arg-type]
        )


def _parallel_group(call_ids: tuple[str, ...], texts: tuple[str, ...]) -> list[object]:
    """One assistant message with several tool calls, then their results in order."""
    return [
        AssistantMessage(
            content=[
                ToolCall(id=call_id, name="read", arguments={"path": f"{call_id}.py"})
                for call_id in call_ids
            ],
            stop_reason="toolUse",
        ),
        *[
            ToolResultMessage(
                tool_call_id=call_id,
                tool_name="read",
                content=[TextContent(text=text)],
            )
            for call_id, text in zip(call_ids, texts, strict=True)
        ],
    ]


def test_two_identical_large_results_in_one_request_share_one_blob(tmp_path: Path) -> None:
    """Content addressing deduplicates the same oversized result inside one request."""
    big = "same payload " * 40
    messages: list[object] = [
        UserMessage(content="start"),
        *_parallel_group(("call-a", "call-b"), (big, big)),
    ]
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=40,
        reserve_tokens=5,
        spill_chars=50,
        spill_preview_chars=20,
        keep_recent_tokens=10,
    )

    prepared = pipeline.prepare(_request(messages))

    digest = hashlib.sha256(big.encode("utf-8")).hexdigest()
    assert "L3" in prepared.layers
    assert [(item.tool_call_id, item.digest) for item in prepared.artifacts] == [
        ("call-a", digest),
        ("call-b", digest),
    ]
    blobs = list((tmp_path / ".run" / "context" / "blobs").glob("*.txt"))
    assert [path.name for path in blobs] == [f"{digest}.txt"]
    assert blobs[0].read_text(encoding="utf-8") == big


def test_a_real_parallel_tool_group_is_never_split(tmp_path: Path) -> None:
    """One assistant message can carry several calls; its results stay atomic."""
    messages: list[object] = [UserMessage(content="root")]
    messages.extend(_parallel_group(("old-1", "old-2"), ("a" * 600, "b" * 600)))
    for index in range(4):
        messages.append(UserMessage(content=f"recent-{index} " + "q" * 200))
        messages.extend(
            _parallel_group((f"tail-{index}-1", f"tail-{index}-2"), ("x" * 400, "y" * 400))
        )
        messages.append(AssistantMessage(content=f"done-{index}"))
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=400,
        reserve_tokens=50,
        spill_chars=10_000,
        keep_recent_tokens=600,
        compact_result_chars=50,
        keep_recent_results=2,
    )

    prepared = pipeline.prepare(_request(messages))
    view = list(prepared.request.messages)

    assert "L1" in prepared.layers and "L2" in prepared.layers
    groups = [
        index
        for index, message in enumerate(view)
        if isinstance(message, AssistantMessage) and len(message.tool_calls) > 1
    ]
    assert groups, "the parallel groups must survive the cut"
    for index in groups:
        calls = view[index].tool_calls
        following = view[index + 1 : index + 1 + len(calls)]
        assert all(isinstance(message, ToolResultMessage) for message in following)
        assert [message.tool_call_id for message in following] == [call.id for call in calls]

    placeholders = [
        message
        for message in view
        if isinstance(message, ToolResultMessage)
        and message.text.startswith("[Earlier read result compacted")
    ]
    assert placeholders, "L2 must rewrite old results instead of dropping them"
    placeholder_ids = {message.tool_call_id for message in placeholders}
    for index in groups:
        call_ids = {call.id for call in view[index].tool_calls}
        assert call_ids & placeholder_ids in (set(), call_ids), "L2 split a parallel group"


def test_stable_prefix_digest_ignores_an_appended_tail(tmp_path: Path) -> None:
    """Only a new persisted summary prefix may move the digest, never a tail entry."""
    pipeline = ContextViewPipeline(cwd=tmp_path, context_window_tokens=10_000, reserve_tokens=100)

    first = pipeline.prepare(_request([UserMessage(content="root request")]))
    grown = pipeline.prepare(
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


def test_stable_prefix_digest_changes_with_a_new_summary_prefix(tmp_path: Path) -> None:
    """A new L4 prefix rewrites the head, so the digest must change with it."""
    pipeline = ContextViewPipeline(cwd=tmp_path, context_window_tokens=10_000, reserve_tokens=100)
    prefix = "Previous conversation summary:\n"

    before = pipeline.prepare(_request([UserMessage(content="root request")]))
    first = pipeline.prepare(
        _request([UserMessage(content=f"{prefix}first summary"), UserMessage(content="tail")])
    )
    second = pipeline.prepare(
        _request([UserMessage(content=f"{prefix}second summary"), UserMessage(content="tail")])
    )

    assert first.stable_prefix_digest != before.stable_prefix_digest
    assert second.stable_prefix_digest != first.stable_prefix_digest


class _ReadOnceProvider:
    """Offline provider that reads one path once, then answers from the tool result."""

    def __init__(self, path: str) -> None:
        self.path = path

    async def stream_response(self, *, messages, **kwargs):
        seen = any(isinstance(message, ToolResultMessage) for message in messages)
        yield AssistantDoneEvent(
            reason="stop" if seen else "toolUse",
            message=AssistantMessage(
                content=(
                    [TextContent(text="read finished")]
                    if seen
                    else [ToolCall(id="read-big", name="read", arguments={"path": self.path})]
                ),
                stop_reason="stop" if seen else "toolUse",
                model="test",
            ),
        )


def _options(tmp_path: Path) -> ApplicationOptions:
    return ApplicationOptions(
        cwd=tmp_path,
        paths=RunAgentPaths(home=tmp_path / "state", agents_home=tmp_path / "agents"),
        model="small-window",
        provider_name="test",
        extensions_enabled=False,
    )


def _settings(context_window: int) -> ProviderSettings:
    return ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("small-window",),
                default_model="small-window",
                api_key_env="CONTEXT_VIEW_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


async def test_a_missing_blob_is_regenerated_from_the_jsonl_history(tmp_path: Path) -> None:
    """Blobs are a derived cache: the durable transcript alone can rebuild them."""
    content = "\n".join("y" * 180 for _ in range(220))
    (tmp_path / "big.txt").write_text(content, encoding="utf-8")
    provider = _ReadOnceProvider("big.txt")
    options = _options(tmp_path)

    async with await CodingApplication.open(
        options, provider=provider, settings=_settings(20_000)
    ) as app:
        await _drain(app.prompt("read the big file"))
        blobs = list((tmp_path / ".run" / "context" / "blobs").glob("*.txt"))
        assert len(blobs) == 1
        stored = blobs[0].read_text(encoding="utf-8")
        assert stored == content

        jsonl = options.paths.project_session_dir(tmp_path) / f"{app.session.session_id}.jsonl"
        assert "y" * 180 in jsonl.read_text(encoding="utf-8")

        blobs[0].unlink()
        await _drain(app.prompt("read the big file again"))
        assert blobs[0].read_text(encoding="utf-8") == stored


def _vanishing_link(monkeypatch: pytest.MonkeyPatch, *, limit: int) -> dict[str, int]:
    """Patch ``os.link`` so the first ``limit`` calls lose their temporary file."""
    real_link = os.link
    calls = {"count": 0}

    def flaky_link(source: str, destination: str) -> None:
        calls["count"] += 1
        if calls["count"] <= limit:
            os.unlink(source)
        real_link(source, destination)

    monkeypatch.setattr("run_agent_coding.context_view.os.link", flaky_link)
    return calls


def test_a_vanished_blob_temporary_file_is_retried_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp file lost before the link is rebuilt instead of failing the request."""
    data = b"retry payload"
    digest = hashlib.sha256(data).hexdigest()
    target = tmp_path / ".run" / "context" / "blobs" / f"{digest}.txt"
    diagnostics = ContextBlobDiagnostics()
    calls = _vanishing_link(monkeypatch, limit=1)

    _write_blob_once(target, data, diagnostics=diagnostics)

    assert calls["count"] == 2
    assert target.read_bytes() == data
    assert diagnostics.attempts == 2
    assert diagnostics.retries == 1
    assert diagnostics.failures == 0
    assert [item.name for item in target.parent.iterdir() if item.name.endswith(".tmp")] == []

    # Idempotent: the same bytes neither rewrite the blob nor raise.
    _write_blob_once(target, data, diagnostics=diagnostics)
    assert diagnostics.attempts == 3
    assert diagnostics.retries == 1
    assert target.read_bytes() == data

    with pytest.raises(RuntimeError, match="digest collision"):
        _write_blob_once(target, b"different bytes", diagnostics=diagnostics)


def test_a_second_vanished_blob_temporary_file_records_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the retry loses its temporary file too, the failure is observable."""
    digest = hashlib.sha256(b"payload").hexdigest()
    target = tmp_path / ".run" / "context" / "blobs" / f"{digest}.txt"
    diagnostics = ContextBlobDiagnostics()
    calls = _vanishing_link(monkeypatch, limit=2)

    with pytest.raises(FileNotFoundError):
        _write_blob_once(target, b"payload", diagnostics=diagnostics)

    assert calls["count"] == 2
    assert not target.exists()
    assert diagnostics.attempts == 2
    assert diagnostics.retries == 1
    assert diagnostics.failures == 1
    assert diagnostics.last_failure_path == str(target)
    assert str(diagnostics.last_failure_error).startswith("FileNotFoundError")


def test_the_context_pipeline_retries_a_vanished_blob_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline keeps its L3 artifact and the default sink counts the retry."""
    big = "z" * 200
    messages: list[object] = [UserMessage(content="start"), *_tool_group("call-retry", big)]
    pipeline = ContextViewPipeline(
        cwd=tmp_path,
        context_window_tokens=40,
        reserve_tokens=5,
        spill_chars=50,
        spill_preview_chars=20,
        keep_recent_tokens=10,
    )
    digest = hashlib.sha256(big.encode("utf-8")).hexdigest()
    before = context_blob_diagnostics().retries
    _vanishing_link(monkeypatch, limit=1)

    prepared = pipeline.prepare(_request(messages))

    blob = tmp_path / ".run" / "context" / "blobs" / f"{digest}.txt"
    assert blob.read_text(encoding="utf-8") == big
    assert "L3" in prepared.layers
    assert prepared.artifacts[0].digest == digest
    assert context_blob_diagnostics().retries == before + 1
