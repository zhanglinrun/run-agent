"""The `session_compact_request` commit channel.

An extension that owns the four-layer decision returns a
`BeforeProviderRequestResult` whose `compaction` field names the durable
compaction it wants: the summary, the retained boundary and the pre-summary
token estimate. The core validates that request against the active branch,
commits it through the same `CompactionEntry` + `LeafEntry` path every other
compaction uses, and ignores an invalid one with a diagnostic instead of
half-committing it. Nothing here talks to a real model: every test drives a fake
provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_view import ContextBudgetExceeded
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_coding.settings import SettingsError, load_settings, settings_from_json
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import CompactionEntry, LeafEntry, MessageEntry

_SUMMARY = "## Goal\nfour-layer summary"

# The extension is loaded like any other: it reads the test's control file on
# every `before_provider_request` call, so a test can arm one exact request (and
# disarm it again) without a second code path.
_EXTENSION_SOURCE = """
import json
from pathlib import Path

from run_agent_coding.extensions.api import (
    BeforeProviderRequestResult,
    CompactionCommitRequest,
)

CONTROL = Path(__CONTROL__)
OBSERVATIONS = Path(__OBSERVATIONS__)


def setup(api):
    def request_compaction(event, context):
        payload = json.loads(CONTROL.read_text(encoding="utf-8"))
        if not payload:
            return None
        return BeforeProviderRequestResult(compaction=CompactionCommitRequest(**payload))

    def record(event, context):
        with OBSERVATIONS.open("a", encoding="utf-8") as stream:
            stream.write("{}:{}:{}\\n".format(event.type, event.reason, event.from_extension))

    api.on("before_provider_request", request_compaction)
    api.on("session_compact", record)
    api.on("session_compact_failed", record)
"""


class _CountingProvider:
    """Fake provider that records whether a request ever reached it."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream_response(self, **kwargs: object) -> AsyncIterator[object]:
        self.requests += 1
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text="ok")],
                model="test",
                provider="test",
                stop_reason="stop",
            ),
        )


def _settings(context_window: int) -> ProviderSettings:
    return ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("test",),
                default_model="test",
                api_key_env="COMPACTION_COMMIT_TEST_API_KEY",
                context_window=context_window,
            ),
        ),
    )


def _write_extension(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Install one control file, one observation log and the extension itself."""
    control = tmp_path / "control.json"
    observations = tmp_path / "observations.log"
    control.write_text("{}", encoding="utf-8")
    extension = tmp_path / "four_layer_commit.py"
    source = _EXTENSION_SOURCE.replace("__CONTROL__", repr(str(control))).replace(
        "__OBSERVATIONS__", repr(str(observations))
    )
    extension.write_text(source, encoding="utf-8")
    return extension, control, observations


def _options(tmp_path: Path, extension: Path) -> ApplicationOptions:
    return replace(options(tmp_path), extension_paths=(extension,))


def _write_strategy_settings(tmp_path: Path, strategy: str) -> None:
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"compaction": {"strategy": strategy}}), encoding="utf-8"
    )


async def _drain(events: AsyncIterator[object]) -> list[object]:
    return [event async for event in events]


def _compactions(session: CodingApplication) -> list[CompactionEntry]:
    return [entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)]


def _plain_request(
    boundary: str,
    *,
    summary: str = _SUMMARY,
    tokens_before: int = 4321,
    trigger: str = "auto",
) -> dict[str, object]:
    return {
        "summary": summary,
        "first_kept_entry_id": boundary,
        "tokens_before": tokens_before,
        "trigger": trigger,
        "metadata": {"layers": ["L2", "L4"]},
    }


async def _grow(app: CodingApplication) -> None:
    """Two complete turns: enough active rows for a real replaced prefix."""
    await _drain(app.prompt("first question"))
    await _drain(app.prompt("second question"))


async def test_an_extension_commit_request_becomes_one_compaction_entry(tmp_path: Path) -> None:
    """The returned request commits the same entry every other compaction writes."""
    extension, control, observations = _write_extension(tmp_path)
    application_options = _options(tmp_path, extension)
    async with await CodingApplication.open(
        application_options, provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        await _grow(app)
        rows = session._active_context_rows()
        assert len(rows) >= 4
        boundary = rows[2][0]
        prefix = [entry_id for entry_id, _message in rows[:2]]
        control.write_text(json.dumps(_plain_request(boundary)), encoding="utf-8")

        events = await _drain(app.prompt("third question"))

        assert events[-1].status == "succeeded"
        compactions = _compactions(session)
        assert len(compactions) == 1, "one request must commit exactly one entry"
        compaction = compactions[0]
        assert compaction.summary == _SUMMARY
        assert compaction.first_kept_entry_id == boundary
        assert compaction.tokens_before == 4321
        assert compaction.replaces_entry_ids == prefix
        # The replaced prefix is gone, the retained tail and the new turn survive.
        active = session._active_context_rows()
        assert active[0][0] == compaction.id
        assert active[1][0] == boundary
        assert session._state.messages[0].text.startswith("Previous conversation summary:")
        assert _SUMMARY in session._state.messages[0].text
        texts = [message.text for message in session._state.messages]
        assert not any("first question" in text for text in texts)
        assert any("second question" in text for text in texts)
        assert any("third question" in text for text in texts)
        # The commit is durable: the session file carries the summary.
        jsonl = (application_options.paths or RunAgentPaths()).project_session_dir(
            tmp_path
        ) / f"{session.session_id}.jsonl"
        assert "four-layer summary" in jsonl.read_text(encoding="utf-8")
        # The observation path reports the extension as the origin, mapped onto
        # the core reason the trigger names.
        assert observations.read_text(encoding="utf-8").splitlines() == [
            "session_compact:threshold:True"
        ]

        # The session stays usable, and the unarmed hook commits nothing further.
        control.write_text("{}", encoding="utf-8")
        follow_up = await _drain(app.prompt("fourth question"))
        assert follow_up[-1].status == "succeeded"
        assert len(_compactions(session)) == 1


def _unknown_boundary(boundary: str) -> dict[str, object]:
    return _plain_request("no-such-entry")


def _blank_summary(boundary: str) -> dict[str, object]:
    return _plain_request(boundary, summary="   ", trigger="manual")


def _non_positive_tokens(boundary: str) -> dict[str, object]:
    return _plain_request(boundary, tokens_before=0, trigger="reactive")


def _unknown_trigger(boundary: str) -> dict[str, object]:
    return _plain_request(boundary, trigger="bogus")


_INVALID_CASES: list[tuple[str, Callable[[str], dict[str, object]], str]] = [
    ("unknown boundary", _unknown_boundary, "first_kept_entry_id"),
    ("blank summary", _blank_summary, "summary must not be empty"),
    ("non-positive tokens", _non_positive_tokens, "tokens_before must be positive"),
    ("unknown trigger", _unknown_trigger, "unknown compaction trigger"),
]


@pytest.mark.parametrize(
    ("factory", "fragment"),
    [(factory, fragment) for _name, factory, fragment in _INVALID_CASES],
    ids=[name for name, _factory, _fragment in _INVALID_CASES],
)
async def test_an_invalid_commit_request_is_diagnosed_and_ignored(
    tmp_path: Path,
    factory: Callable[[str], dict[str, object]],
    fragment: str,
) -> None:
    """A rejected request writes nothing, says why, and leaves the session usable."""
    extension, control, _observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        await _grow(app)
        boundary = session._active_context_rows()[2][0]
        control.write_text(json.dumps(factory(boundary)), encoding="utf-8")

        events = await _drain(app.prompt("third question"))

        assert events[-1].status == "succeeded"
        assert _compactions(session) == [], "an invalid request must never half-commit"
        assert session.last_diagnostic_log_path is not None
        diagnostic = Path(session.last_diagnostic_log_path).read_text(encoding="utf-8")
        assert "session_compact_request" in diagnostic
        assert fragment in diagnostic

        # Still usable, still committable: the same session takes a valid request.
        rows = session._active_context_rows()
        control.write_text(json.dumps(_plain_request(rows[1][0])), encoding="utf-8")
        follow_up = await _drain(app.prompt("fourth question"))
        assert follow_up[-1].status == "succeeded"
        assert len(_compactions(session)) == 1


async def test_the_commit_channel_is_not_gated_by_the_strategy(tmp_path: Path) -> None:
    """`summary-only` still lets an extension commit through the same channel."""
    _write_strategy_settings(tmp_path, "summary-only")
    extension, control, _observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        assert session._config.compaction_strategy == "summary-only"
        await _grow(app)
        rows = session._active_context_rows()
        control.write_text(
            json.dumps(_plain_request(rows[2][0], trigger="manual")), encoding="utf-8"
        )

        await _drain(app.prompt("third question"))

        compactions = _compactions(session)
        assert [entry.first_kept_entry_id for entry in compactions] == [rows[2][0]]


def test_four_layer_parses_round_trips_and_is_project_configurable(tmp_path: Path) -> None:
    settings = settings_from_json({"compaction": {"strategy": "four-layer"}})
    assert settings.compaction_strategy == "four-layer"
    assert settings.to_json() == {"compaction": {"enabled": True, "strategy": "four-layer"}}
    assert settings_from_json(settings.to_json()) == settings

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    (project / ".run").mkdir(parents=True)
    (project / ".run" / "settings.json").write_text(
        json.dumps({"compaction": {"strategy": "four-layer"}}), encoding="utf-8"
    )
    assert load_settings(RunAgentPaths(home=home), project).compaction_strategy == "four-layer"


@pytest.mark.parametrize("strategy", ["magic", "fourlayer", "four layers", "CHEAP-FIRST"])
def test_unknown_strategies_are_still_rejected(strategy: str) -> None:
    with pytest.raises(SettingsError, match="compaction.strategy"):
        settings_from_json({"compaction": {"strategy": strategy}})


async def test_four_layer_keeps_the_hard_window_guard(tmp_path: Path) -> None:
    """The core prepares nothing under `four-layer`, but still refuses an oversized view."""
    _write_strategy_settings(tmp_path, "four-layer")
    provider = _CountingProvider()
    async with await CodingApplication.open(
        options(tmp_path), provider=provider, settings=_settings(2_000)
    ) as app:
        session = app.session
        assert session._config.compaction_strategy == "four-layer"
        with pytest.raises(ContextBudgetExceeded, match="persistent L4 compaction is required"):
            await _drain(app.prompt("x" * 40_000))
        assert _compactions(session) == [], "the core must not summarize on its own"

    assert provider.requests == 0, "an over-limit request must never reach the provider"


async def test_four_layer_does_not_auto_compact_behind_the_extension(tmp_path: Path) -> None:
    """A pinned threshold is inert under `four-layer`: the extension owns L4."""
    _write_strategy_settings(tmp_path, "four-layer")
    async with await CodingApplication.open(
        options(tmp_path), provider=ReplyProvider(), settings=_settings(60_000)
    ) as app:
        session = app.session
        for index in range(3):
            await _drain(app.prompt(f"turn {index} " + "x" * 20_000))
        session._auto_compact_token_threshold = 1_000
        await _drain(app.prompt("one more turn " + "x" * 20_000))
        assert _compactions(session) == []


async def test_a_committed_compaction_keeps_the_message_entries(tmp_path: Path) -> None:
    """The commit appends one `CompactionEntry` + `LeafEntry`; no message is rewritten."""
    extension, control, _observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        await _grow(app)
        before = {
            entry.id for entry in session._entries.values() if isinstance(entry, MessageEntry)
        }
        rows = session._active_context_rows()
        control.write_text(json.dumps(_plain_request(rows[2][0])), encoding="utf-8")

        await _drain(app.prompt("third question"))

        entries = list(session._entries.values())
        after = {entry.id for entry in entries if isinstance(entry, MessageEntry)}
        assert before <= after, "no earlier message entry may disappear"
        assert len(after) > len(before), "the new turn's messages are still durable"
        compaction = _compactions(session)[0]
        leaf = next(
            entry
            for entry in entries
            if isinstance(entry, LeafEntry) and entry.entry_id == compaction.id
        )
        assert leaf.parent_id == compaction.id, "the leaf pair commits atomically with the entry"
