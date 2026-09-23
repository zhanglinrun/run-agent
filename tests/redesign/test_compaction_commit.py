"""The `session_compact_request` commit channel.

An extension that owns compaction returns a `BeforeProviderRequestResult` whose
`compaction` field names the durable compaction it wants: the summary, the
retained boundary and the pre-summary token estimate. The core validates that
request against the active branch, commits it through the same `CompactionEntry`
+ `LeafEntry` path every other compaction uses, and ignores an invalid one with a
diagnostic instead of half-committing it. The core itself never compacts: no
threshold, no summarizer and no strategy key is consulted anywhere here. Nothing
in this module talks to a real model: every test drives a fake provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.context_budget import ContextBudgetExceeded
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import (
    OpenAICompatibleProviderConfig,
    ProviderSettings,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.contracts import SessionConflict
from run_agent_core.session.entries import CompactionEntry, LeafEntry, MessageEntry

_SUMMARY = "## Goal\nlayered summary"

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
from run_agent_core.messages import UserMessage
from run_agent_core.provider import ModelRequest

CONTROL = Path(__CONTROL__)
OBSERVATIONS = Path(__OBSERVATIONS__)


def setup(api):
    def request_compaction(event, context):
        payload = json.loads(CONTROL.read_text(encoding="utf-8"))
        if not payload:
            return None
        rewrite = payload.pop("rewrite", None)
        commit = CompactionCommitRequest(**payload)
        if rewrite is None:
            return BeforeProviderRequestResult(compaction=commit)
        request = event.payload
        return BeforeProviderRequestResult(
            request=ModelRequest(
                model=request.model,
                system=request.system,
                messages=(*request.messages, UserMessage(content=rewrite)),
                tools=request.tools,
                session_id=request.session_id,
            ),
            compaction=commit,
        )

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
    extension = tmp_path / "extension_commit.py"
    source = _EXTENSION_SOURCE.replace("__CONTROL__", repr(str(control))).replace(
        "__OBSERVATIONS__", repr(str(observations))
    )
    extension.write_text(source, encoding="utf-8")
    return extension, control, observations


def _options(tmp_path: Path, extension: Path) -> ApplicationOptions:
    return replace(options(tmp_path), extension_paths=(extension,))


def _write_legacy_compaction_settings(tmp_path: Path, strategy: str) -> None:
    """A pre-existing file keeps the retired `compaction.strategy` key."""
    home = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"compaction": {"enabled": True, "strategy": strategy}}), encoding="utf-8"
    )


async def _drain(events: AsyncIterator[object]) -> list[object]:
    return [event async for event in events]


def _compactions(session: CodingApplication) -> list[CompactionEntry]:
    return [entry for entry in session._entries.values() if isinstance(entry, CompactionEntry)]


async def _prefix_digest(session: CodingApplication) -> str:
    assert session.current_snapshot_id is not None
    snapshot = await session.storage.get_snapshot(session.current_snapshot_id)
    return str(snapshot["payload"]["generation_controls"]["context_view"]["stable_prefix_digest"])


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
        "metadata": {"layers": ["L2", "L3"]},
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
        assert "layered summary" in jsonl.read_text(encoding="utf-8")
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


async def test_an_oversized_view_is_refused_before_the_provider_is_called(
    tmp_path: Path,
) -> None:
    """The core prepares nothing, but still refuses a request above the window."""
    provider = _CountingProvider()
    async with await CodingApplication.open(
        options(tmp_path), provider=provider, settings=_settings(2_000)
    ) as app:
        session = app.session
        with pytest.raises(ContextBudgetExceeded, match="persistent compaction is required"):
            await _drain(app.prompt("x" * 40_000))
        assert _compactions(session) == [], "the core must not summarize on its own"

    assert provider.requests == 0, "an over-limit request must never reach the provider"


async def test_the_core_never_compacts_on_its_own(tmp_path: Path) -> None:
    """A long session with a legacy settings key still writes no core compaction."""
    _write_legacy_compaction_settings(tmp_path, "four-layer")
    async with await CodingApplication.open(
        options(tmp_path), provider=ReplyProvider(), settings=_settings(60_000)
    ) as app:
        for index in range(4):
            await _drain(app.prompt(f"turn {index} " + "x" * 20_000))

        assert _compactions(app.session) == []


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


async def test_the_summary_prefix_is_stable_until_the_next_commit(tmp_path: Path) -> None:
    """A growing tail keeps the prefix and its digest; a new commit rewrites both."""
    extension, control, _observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        await _grow(app)
        rows = session._active_context_rows()
        control.write_text(
            json.dumps(_plain_request(rows[2][0], summary="## Goal\nfirst summary")),
            encoding="utf-8",
        )

        await _drain(app.prompt("third question"))

        assert len(_compactions(session)) == 1
        control.write_text("{}", encoding="utf-8")

        # One quiet turn after the commit: the prefix and its digest must agree.
        await _drain(app.prompt("fourth question"))
        prefix = session._state.messages[0].text
        digest = await _prefix_digest(session)
        assert prefix.startswith("Previous conversation summary:")
        assert "first summary" in prefix

        # A tail append alone must not move the persisted prefix or its digest.
        await _drain(app.prompt("fifth question"))
        assert len(_compactions(session)) == 1, "no new summary was needed"
        assert session._state.messages[0].text == prefix
        assert await _prefix_digest(session) == digest

        # The next commit writes a new prefix, so the digest must change with it.
        rows = session._active_context_rows()
        control.write_text(
            json.dumps(
                _plain_request(rows[2][0], summary="## Goal\nsecond summary", trigger="manual")
            ),
            encoding="utf-8",
        )
        await _drain(app.prompt("sixth question"))
        control.write_text("{}", encoding="utf-8")
        await _drain(app.prompt("seventh question"))

        assert len(_compactions(session)) == 2
        assert session._state.messages[0].text != prefix
        assert "second summary" in session._state.messages[0].text
        assert await _prefix_digest(session) != digest


async def test_an_oversized_view_is_refused_after_an_extension_rewrote_it(
    tmp_path: Path,
) -> None:
    """The guard decides on the rewritten view: a rewrite cannot unlock a commit."""
    provider = _CountingProvider()
    extension, control, observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=provider, settings=_settings(20_000)
    ) as app:
        session = app.session
        await _drain(app.prompt("first question"))
        rows = session._active_context_rows()
        assert len(rows) >= 2, "the first turn grew an active branch"
        request = _plain_request(rows[1][0], summary="## Goal\nnever committed")
        request["rewrite"] = "z" * 100_000
        control.write_text(json.dumps(request), encoding="utf-8")
        requests_before = provider.requests

        with pytest.raises(ContextBudgetExceeded, match="persistent compaction is required"):
            await _drain(app.prompt("second question"))

        assert provider.requests == requests_before, "the refused view never reached the provider"
        assert _compactions(session) == [], (
            "the requested commit was dropped with the refused request"
        )
        assert not observations.exists() or observations.read_text(encoding="utf-8") == "", (
            "no compaction event is reported for a request that was never sent"
        )


async def test_a_cas_conflict_on_a_commit_is_diagnosed_without_failing_the_run(
    tmp_path: Path,
) -> None:
    """A conflicting compaction write is reported on `session_compact_failed` only."""
    extension, control, observations = _write_extension(tmp_path)
    async with await CodingApplication.open(
        _options(tmp_path, extension), provider=ReplyProvider(), settings=_settings(200_000)
    ) as app:
        session = app.session
        await _grow(app)
        rows = session._active_context_rows()
        control.write_text(json.dumps(_plain_request(rows[2][0])), encoding="utf-8")

        storage = session.storage
        committed = storage.append_entries

        async def conflicting(entries, *, expected_head, token):
            if any(isinstance(entry, CompactionEntry) for entry in entries):
                raise SessionConflict("head moved: another writer appended first")
            return await committed(entries, expected_head=expected_head, token=token)

        storage.append_entries = conflicting  # type: ignore[method-assign]

        events = await _drain(app.prompt("third question"))

        assert events[-1].status == "succeeded", "the run must not fail with the commit"
        assert _compactions(session) == [], "the conflicting entry was never written"
        assert session.last_diagnostic_log_path is not None
        diagnostic = Path(session.last_diagnostic_log_path).read_text(encoding="utf-8")
        assert "session_compact_request" in diagnostic
        assert "head moved" in diagnostic
        assert observations.read_text(encoding="utf-8").splitlines() == [
            "session_compact_failed:threshold:True"
        ], "one failure event, reported as an extension compaction under the core reason"

        # The session stays usable, and a later commit still works.
        storage.append_entries = committed  # type: ignore[method-assign]
        rows = session._active_context_rows()
        control.write_text(json.dumps(_plain_request(rows[1][0])), encoding="utf-8")
        follow_up = await _drain(app.prompt("fourth question"))
        assert follow_up[-1].status == "succeeded"
        assert len(_compactions(session)) == 1
