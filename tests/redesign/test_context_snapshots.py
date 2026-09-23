"""The durable context snapshot is exactly the Provider view that was sent.

`技术点三` requires `_record_context_snapshot` to observe the final Provider view
plus the guard's measurements: an auditor must be able to compare what the model
saw with what the host claims it sent. These tests read the snapshot back through
the session's own storage handle and compare it with the offline provider's
recorded request. Compaction belongs to an extension, so the core report carries
measurements only - no layers, artifacts or rewrite flags.
"""

from __future__ import annotations

from pathlib import Path

from run_agent_coding.application import ApplicationOptions, CodingApplication
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
)
from run_agent_core.provider_events import AssistantDoneEvent

_REPORT_KEYS = {"tokens_before", "tokens_after", "stable_prefix_digest"}


class _RecordingProvider:
    """Offline provider that records every request it is asked to send."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.requests: list[dict] = []

    async def stream_response(self, *, model, system, messages, tools, **kwargs):
        self.requests.append(
            {
                "system": system,
                "messages": [message.model_dump(mode="json") for message in messages],
            }
        )
        seen = any(isinstance(message, ToolResultMessage) for message in messages)
        if self.path is not None and not seen:
            yield AssistantDoneEvent(
                reason="toolUse",
                message=AssistantMessage(
                    content=[ToolCall(id="read-big", name="read", arguments={"path": self.path})],
                    stop_reason="toolUse",
                    model="test",
                ),
            )
            return
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="done")],
                model="test",
                provider="test",
                stop_reason="stop",
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
                api_key_env="CONTEXT_SNAPSHOT_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


async def _drain(events) -> list[object]:
    return [event async for event in events]


async def _agent_snapshot(session) -> dict:
    assert session.current_snapshot_id is not None
    return await session.storage.get_snapshot(session.current_snapshot_id)


async def test_snapshot_is_the_final_provider_view_with_the_guard_report(
    tmp_path: Path,
) -> None:
    """The recorded input is the view the provider received, plus its measurements."""
    content = "\n".join("y" * 180 for _ in range(220))
    (tmp_path / "big.txt").write_text(content, encoding="utf-8")
    provider = _RecordingProvider("big.txt")

    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(20_000)
    ) as app:
        await _drain(app.prompt("read the big file"))
        snapshot = await _agent_snapshot(app.session)
        payload = snapshot["payload"]
        report = payload["generation_controls"]["context_view"]

        sent = provider.requests[-1]
        assert payload["system"] == sent["system"]
        assert payload["messages"] == sent["messages"]

        assert set(report) == _REPORT_KEYS
        assert report["tokens_before"] == report["tokens_after"], "the core rewrites nothing"
        assert report["tokens_after"] <= 20_000
        assert len(report["stable_prefix_digest"]) == 64

        observed = [message for message in payload["messages"] if message["role"] == "toolResult"]
        assert len(observed) == 1
        assert observed[0]["content"][0]["text"] == content
        raw = [
            message.text
            for message in app.session.messages
            if isinstance(message, ToolResultMessage)
        ]
        assert raw == [content], "the durable transcript keeps the full result"
        assert not (tmp_path / ".run" / "context").exists(), "the core spills nothing to disk"


async def test_a_small_view_snapshot_reports_only_measurements(tmp_path: Path) -> None:
    """A view inside the budget is observed as-is: measurements, nothing else."""
    provider = _RecordingProvider()

    async with await CodingApplication.open(_options(tmp_path), provider=provider) as app:
        await _drain(app.prompt("small question"))
        snapshot = await _agent_snapshot(app.session)
        payload = snapshot["payload"]
        report = payload["generation_controls"]["context_view"]

        assert payload["messages"] == provider.requests[-1]["messages"]
        assert set(report) == _REPORT_KEYS
        assert report["tokens_before"] == report["tokens_after"]
        assert len(report["stable_prefix_digest"]) == 64
