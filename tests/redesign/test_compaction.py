"""Persistent compaction: boundary metadata and summary-prefix identity.

`技术点三` L4 keeps one durable `CompactionEntry` per summary. These tests lock that
the automatic paths record the same metadata as the manual one:

* `first_kept_entry_id` names the durable entry the summary stops before, and
  `tokens_before` the pre-summary estimate;
* the summary prefix in the Provider view is stable while the tail grows - only a
  new L4 rewrites it, which is exactly when `stable_prefix_digest` must move.
"""

from __future__ import annotations

from pathlib import Path

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_window import SUMMARIZATION_SYSTEM_PROMPT
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry

# Above `keep_recent_tokens + DEFAULT_COMPACTION_RESERVE_TOKENS` (36_384): the free view
# fits, so the pinned caller threshold is the only reason to summarize.
_WINDOW = 60_000
_TURN_CHARS = 20_000


class _SummarizerProvider:
    """Offline provider whose summaries are counter-tagged and therefore unique."""

    def __init__(self) -> None:
        self.summaries = 0

    async def stream_response(self, *, model, system, messages, **kwargs):
        summarizing = system.startswith(SUMMARIZATION_SYSTEM_PROMPT)
        if summarizing:
            self.summaries += 1
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[
                    TextContent(
                        text=f"## Goal\ncondensed-{self.summaries}" if summarizing else "ok"
                    )
                ],
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
                api_key_env="CONTEXT_COMPACTION_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def _compactions(session) -> list[CompactionEntry]:
    return [entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)]


async def _drain(events) -> list[object]:
    return [event async for event in events]


async def _grow_history(app, *, turns: int) -> None:
    """Build a history that can be summarized, without paying for a summary yet."""
    app.session.set_auto_compaction_enabled(False)
    for index in range(turns):
        await _drain(app.prompt(f"turn {index} " + "x" * _TURN_CHARS))


def _force_auto_compaction(session) -> None:
    session.set_auto_compaction_enabled(True)
    session._auto_compact_token_threshold = 1_000


async def _prefix_digest(session) -> str:
    assert session.current_snapshot_id is not None
    snapshot = await session.storage.get_snapshot(session.current_snapshot_id)
    return str(snapshot["payload"]["generation_controls"]["context_view"]["stable_prefix_digest"])


async def test_auto_compaction_records_the_boundary_and_the_pre_summary_tokens(
    tmp_path: Path,
) -> None:
    """A threshold compaction names its first kept entry and its token prior."""
    provider = _SummarizerProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app, turns=4)
        plan = session._recent_preserving_compaction_plan()
        assert plan is not None
        rows = session._active_context_rows()
        expected_boundary = rows[len(plan.replace_entry_ids)][0]
        tokens_before = session.context_token_estimate
        _force_auto_compaction(session)

        assert await session._maybe_auto_compact() is True

        compaction = _compactions(session)[-1]
        assert compaction.replaces_entry_ids == list(plan.replace_entry_ids)
        assert compaction.first_kept_entry_id == expected_boundary
        assert compaction.tokens_before == tokens_before

        active = session._active_context_rows()
        assert active[0][0] == compaction.id
        assert active[1][0] == expected_boundary
        assert session._state.messages[0].text.startswith("Previous conversation summary:")


async def test_overflow_compaction_records_the_boundary_and_the_pre_summary_tokens(
    tmp_path: Path,
) -> None:
    """The overflow retry path records the same metadata as the threshold path."""
    provider = _SummarizerProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app, turns=4)
        plan = session._recent_preserving_compaction_plan()
        assert plan is not None
        rows = session._active_context_rows()
        expected_boundary = rows[len(plan.replace_entry_ids)][0]
        tokens_before = session.context_token_estimate

        assert await session._try_overflow_compact(context=session._diagnostic_context()) is True

        compaction = _compactions(session)[-1]
        assert compaction.replaces_entry_ids == list(plan.replace_entry_ids)
        assert compaction.first_kept_entry_id == expected_boundary
        assert compaction.tokens_before == tokens_before


async def test_the_summary_prefix_is_stable_until_the_next_l4(tmp_path: Path) -> None:
    """A growing tail keeps the prefix and its digest; a new L4 rewrites both."""
    provider = _SummarizerProvider()
    async with await CodingApplication.open(
        _options(tmp_path), provider=provider, settings=_settings(_WINDOW)
    ) as app:
        session = app.session
        await _grow_history(app, turns=5)
        pre_l4_digest = await _prefix_digest(session)
        _force_auto_compaction(session)
        await _drain(app.prompt("turn 5 " + "x" * _TURN_CHARS))

        compactions = _compactions(session)
        assert compactions, "the pinned threshold must have written a summary"
        assert compactions[-1].summary in session._state.messages[0].text

        # One quiet turn after the summaries settled: prefix and digest must agree.
        session._auto_compact_token_threshold = 10**9
        await _drain(app.prompt("small follow-up"))
        settled = len(_compactions(session))
        first = _compactions(session)[-1]
        prefix = session._state.messages[0].text
        digest = await _prefix_digest(session)
        assert prefix.startswith("Previous conversation summary:")
        assert digest != pre_l4_digest, "an L4 summary head is a different prefix"

        # A tail append alone must not move the persisted prefix or its digest.
        await _drain(app.prompt("another small follow-up"))
        assert len(_compactions(session)) == settled, "no new summary was needed"
        assert _compactions(session)[-1].id == first.id
        assert session._state.messages[0].text == prefix
        assert await _prefix_digest(session) == digest

        # The next real L4 writes a new prefix, so the digest must change with it.
        _force_auto_compaction(session)
        await _drain(app.prompt("turn 7 " + "x" * _TURN_CHARS))
        assert len(_compactions(session)) > settled
        second = _compactions(session)[-1]
        assert second.id != first.id
        assert second.summary != first.summary
        assert session._state.messages[0].text != prefix
        assert await _prefix_digest(session) != digest
