"""L4 must be reachable when the free cheap-first view cannot fit.

`ContextViewPipeline.prepare` reports `needs_l4` once the free L3/L1/L2 view still
exceeds `context_window - reserve`. Automatic compaction used to decide purely on
the caller's threshold, so whenever that threshold is *looser* than
`window - reserve` the flag was ignored: the session kept sending an oversized
provider view and only `require_hard_limit` (or a provider overflow) stopped it.

The gap only exists for windows below `keep_recent_tokens + reserve` (20_000 +
16_384 = 36_384), because above that the free view's own recent-window limit is
what bounds the request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry

# Window below keep_recent_tokens + DEFAULT_COMPACTION_RESERVE_TOKENS (36_384) so the
# free view can genuinely exceed window - reserve.
_SMALL_WINDOW = 30_000
_TURN_CHARS = 20_000
_TURNS = 6


class ReplyProvider:
    """Offline provider: summarization gets a summary, everything else an answer."""

    async def stream_response(self, *, messages, **kwargs):
        summarization = "summarization" in str(kwargs.get("system", "")).lower()
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="## Goal\ncondensed" if summarization else "ok")],
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
                api_key_env="CONTEXT_L4_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def _compactions(session) -> list[CompactionEntry]:
    return [entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)]


async def _drain(events) -> list[object]:
    return [event async for event in events]


@pytest.mark.asyncio
async def test_free_view_within_budget_skips_summarization(tmp_path: Path) -> None:
    """A short conversation fits the free view: nobody pays for a summary."""
    async with await CodingApplication.open(
        _options(tmp_path), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        for index in range(3):
            await _drain(app.prompt(f"short question {index}"))
        assert _compactions(app.session) == []


@pytest.mark.asyncio
async def test_needs_l4_summarizes_even_when_threshold_is_looser(tmp_path: Path) -> None:
    """A looser caller threshold must not mask `needs_l4`.

    Provider-discovered limits default to 90% of the window, which is above
    `window - reserve`; the threshold is pinned even higher here to make the
    `needs_l4` branch the only reason left to summarize.
    """
    async with await CodingApplication.open(
        _options(tmp_path), provider=ReplyProvider(), settings=_settings(_SMALL_WINDOW)
    ) as app:
        session = app.session
        assert session.context_window_tokens == _SMALL_WINDOW
        session._auto_compact_token_threshold = 10**9
        for index in range(_TURNS):
            await _drain(app.prompt(f"turn {index} " + "x" * _TURN_CHARS))
            if _compactions(session):
                break
        compactions = _compactions(session)
        assert compactions, (
            "the free view exceeded window - reserve, so the automatic path must "
            "append a persistent CompactionEntry instead of only reporting needs_l4"
        )
        # The automatic path carries the same boundary metadata as the manual one.
        compaction = compactions[-1]
        assert compaction.first_kept_entry_id is not None
        assert compaction.tokens_before is not None
        assert compaction.tokens_before > 0
        assert compaction.first_kept_entry_id not in set(compaction.replaces_entry_ids)
        rows = session._active_context_rows()
        assert rows[0][0] == compaction.id
        assert compaction.first_kept_entry_id in {entry_id for entry_id, _ in rows}
        if len(rows) > 1:
            assert rows[1][0] == compaction.first_kept_entry_id
