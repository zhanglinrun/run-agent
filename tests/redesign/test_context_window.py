"""The context window is a hard boundary: no over-limit request leaves the host.

`ContextBudgetGuard` is the last gate before physical I/O. These tests drive the
real `CodingSession` with an offline provider and lock the failure contract of
`技术点三` 分层压缩:

* a request that still exceeds the model window is refused with
  `ContextBudgetExceeded` *before* the provider is called - the provider sees
  zero requests, not a truncated one;
* nothing in the core tries to shrink the request on its own: a refused turn
  writes no `CompactionEntry` and no summarization request is ever sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_budget import ContextBudgetExceeded
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry


class _RecordingProvider:
    """Offline provider: the agent's own requests are counted, naming is not."""

    def __init__(self) -> None:
        self.kinds = 0
        self.agent_calls = 0

    async def stream_response(self, *, tools=(), **kwargs: object) -> object:
        self.kinds += 1
        if tools:
            # Session naming sends no tools, so this counts the agent's requests only.
            self.agent_calls += 1
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="ok")],
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
                api_key_env="CONTEXT_WINDOW_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def _compactions(session) -> list[CompactionEntry]:
    return [entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)]


async def _drain(events) -> list[object]:
    return [event async for event in events]


async def test_an_over_window_request_raises_before_any_provider_call(
    tmp_path: Path,
) -> None:
    """An oversized request is refused, never truncated or summarized by the core."""
    provider = _RecordingProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(2_000)
    ) as app:
        with pytest.raises(ContextBudgetExceeded, match="persistent compaction is required"):
            await _drain(app.prompt("x" * 40_000))

        assert _compactions(app.session) == [], "the core must not summarize on its own"

    assert provider.kinds == 0, "an over-limit request must never reach the provider"


async def test_a_request_inside_the_window_reaches_the_provider(tmp_path: Path) -> None:
    """The guard rejects only what does not fit: a small turn is sent unchanged."""
    provider = _RecordingProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(20_000)
    ) as app:
        events = await _drain(app.prompt("small question"))

        assert events[-1].status == "succeeded"
        assert _compactions(app.session) == []

    assert provider.agent_calls == 1, "the small turn must reach the provider"
    assert provider.kinds >= 1
