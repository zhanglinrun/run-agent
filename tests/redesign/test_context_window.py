"""The context window is a hard boundary: no over-limit request leaves the host.

`ContextViewPipeline` is the last gate before physical I/O. These tests drive the
real `CodingSession` with an offline provider and lock the failure contract of
`技术点三` L4:

* a free L3/L1/L2 view that is still larger than `context_window - reserve` is
  refused with `ContextBudgetExceeded` *before* the provider is called - the
  provider sees zero requests, not a truncated one;
* a summarization failure (exception or empty summary) is diagnosed and the turn
  continues while the free view stays inside the hard window;
* cancellation during summarization is not swallowed;
* a failed summary does not downgrade the guard: an unsalvageable free view still
  raises before the oversized provider request.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_view import ContextBudgetExceeded
from run_agent_coding.context_window import SUMMARIZATION_SYSTEM_PROMPT
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry

# Above `keep_recent_tokens + DEFAULT_COMPACTION_RESERVE_TOKENS` (36_384) so the free
# view's own recent-window limit bounds the request and a failed summary still leaves
# the turn inside the hard window.
_WINDOW = 60_000
_TURN_CHARS = 20_000
_TURNS = 4


class _RecordingProvider:
    """Offline provider: agent replies "ok", summaries are fault-injectable."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.kinds: list[str] = []
        self.entered = asyncio.Event()

    async def stream_response(self, *, model, system, messages, **kwargs):
        # The cwd of a failing test can itself contain "summarization", so the
        # discriminator is the summarizer's own system prompt, not a substring.
        summarizing = system.startswith(SUMMARIZATION_SYSTEM_PROMPT)
        self.kinds.append("summary" if summarizing else "agent")
        if summarizing:
            if self.mode == "raise":
                raise RuntimeError("injected summarization failure")
            if self.mode == "empty":
                yield AssistantDoneEvent(
                    reason="stop",
                    message=AssistantMessage(
                        content=[TextContent(text="   ")],
                        model="test",
                        provider="test",
                        stop_reason="stop",
                    ),
                )
                return
            if self.mode == "hang":
                self.entered.set()
                await asyncio.Event().wait()
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="## Goal\ncondensed" if summarizing else "ok")],
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


async def _grow_history(app, *, turns: int = _TURNS) -> None:
    """Build a history that needs the free view without paying for a summary."""
    app.session.set_auto_compaction_enabled(False)
    for index in range(turns):
        await _drain(app.prompt(f"turn {index} " + "x" * _TURN_CHARS))


def _force_auto_compaction(session) -> None:
    session.set_auto_compaction_enabled(True)
    session._auto_compact_token_threshold = 1_000


async def test_a_free_view_over_the_window_raises_before_any_provider_call(
    tmp_path: Path,
) -> None:
    """The free layers cannot shrink one oversized request, so nothing is sent."""
    provider = _RecordingProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(2_000)
    ) as app:
        with pytest.raises(ContextBudgetExceeded, match="persistent L4 compaction is required"):
            await _drain(app.prompt("x" * 40_000))

    assert provider.kinds == [], "an over-limit request must never reach the provider"


async def test_a_failed_summarization_is_diagnosed_and_the_free_view_still_runs(
    tmp_path: Path,
) -> None:
    """One summarization exception must not lose the turn when the free view fits."""
    provider = _RecordingProvider("raise")
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app)
        _force_auto_compaction(session)

        events = await _drain(app.prompt("keep going"))

        assert events[-1].status == "succeeded"
        assert _compactions(session) == []
        assert session._last_diagnostic_log_path is not None
        log = Path(session._last_diagnostic_log_path)
        assert log.is_file()
        text = log.read_text(encoding="utf-8")
        assert "auto_compact_before_prompt" in text
        assert "injected summarization failure" in text

    assert "summary" in provider.kinds, "the summarization must have been attempted"
    assert "agent" in provider.kinds, "the free view must still be sent"


async def test_an_empty_summary_is_a_diagnosed_failure(tmp_path: Path) -> None:
    """An empty summary is a failure, not a silently persisted empty prefix."""
    provider = _RecordingProvider("empty")
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app)
        _force_auto_compaction(session)

        events = await _drain(app.prompt("keep going"))

        assert events[-1].status == "succeeded"
        assert _compactions(session) == []
        assert session._last_diagnostic_log_path is not None
        text = Path(session._last_diagnostic_log_path).read_text(encoding="utf-8")
        assert "empty summary" in text
        assert "auto_compact_before_prompt" in text


async def test_cancellation_during_summarization_propagates(tmp_path: Path) -> None:
    """Cancelling the summarization cancels the run; no compaction is appended."""
    provider = _RecordingProvider("hang")
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app)
        _force_auto_compaction(session)
        before = list(provider.kinds)

        task = asyncio.create_task(_drain(app.prompt("cancel me")))
        await asyncio.wait_for(provider.entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _compactions(session) == []

    assert provider.kinds[len(before) :] == ["summary"], "no agent request was made"


async def test_an_unsalvageable_free_view_is_refused_after_a_failed_summary(
    tmp_path: Path,
) -> None:
    """A failed summary must not turn into an over-limit provider request."""
    provider = _RecordingProvider("raise")
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app)
        _force_auto_compaction(session)
        before = list(provider.kinds)

        with pytest.raises(ContextBudgetExceeded, match="persistent L4 compaction is required"):
            await _drain(app.prompt("y" * 300_000))

        assert provider.kinds[len(before) :] == ["summary"]
        assert _compactions(session) == []
