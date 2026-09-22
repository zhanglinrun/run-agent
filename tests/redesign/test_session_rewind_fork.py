"""End-to-end `/rewind` and `/fork` over a live application and its JSONL files.

The plan's durable-tree promises - rewind only appends a leaf, fork copies the complete
target path including resource activation, compaction and run commits - are only real if
they hold through the application command path and survive a reopen. These tests drive the
real application with a local provider stub, so no provider is contacted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from run_agent_core.messages import AgentMessage, UserMessage
from run_agent_core.session.entries import (
    CompactionEntry,
    CustomEntry,
    LeafEntry,
    RunCommitEntry,
    SessionEntry,
)
from run_agent_core.session.jsonl import entry_from_json_line
from run_agent_core.session.storage import JsonlSessionStorage
from run_agent_core.session.tree import path_to_entry, resolve_active_leaf_id


class LocalProvider(ReplyProvider):
    async def aclose(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderSettings:
    """Register the local endpoint under the name sessions remember, as a host would."""
    monkeypatch.setenv("RUN_SESSION_TEST_API_KEY", "local-test-only")
    configured = ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("test",),
                default_model="test",
                api_key_env="RUN_SESSION_TEST_API_KEY",
            ),
        ),
    )
    monkeypatch.setattr(
        "run_agent_coding.session._create_runtime_provider",
        lambda *args, **kwargs: LocalProvider(),
    )
    monkeypatch.setattr("run_agent_coding.session.load_provider_settings", lambda *args: configured)
    return configured


def _session_path(tmp_path: Path, session_id: str) -> Path:
    return options(tmp_path).paths.project_session_dir(tmp_path) / f"{session_id}.jsonl"


def _stored_entries(path: Path) -> list[SessionEntry]:
    return [
        entry_from_json_line(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


_SUMMARY_PREFIX = "Previous conversation summary:"


def _user_texts(messages: Sequence[AgentMessage]) -> list[str]:
    """User turns only: the L4 compaction summary also travels as a user message."""
    return [
        message.text
        for message in messages
        if isinstance(message, UserMessage) and not message.text.startswith(_SUMMARY_PREFIX)
    ]


async def test_rewind_keeps_the_abandoned_branch_readable(
    tmp_path: Path, settings: ProviderSettings
) -> None:
    async with await CodingApplication.open(
        options(tmp_path), provider=LocalProvider(), settings=settings
    ) as app:
        await app.start()
        first = [event async for event in app.prompt("first task")]
        first_head = first[-1].head_id
        second = [event async for event in app.prompt("second task")]
        second_head = second[-1].head_id
        session_id = app.session.session_id
        before_ids = [entry.id for entry in (await app.session.storage.read_entries()).entries]

        result = await app.command(f"/rewind {first_head}")

        assert result.handled
        assert first_head in result.message
        assert (await app.session.storage.get_head()).entry_id == first_head
        assert _user_texts(app.session.messages) == ["first task"]
        stored = _stored_entries(_session_path(tmp_path, session_id))
        # Append-only: the abandoned branch is still on disk and still traversable.
        assert {entry.id for entry in stored} >= set(before_ids)
        abandoned = [entry.id for entry in path_to_entry(stored, second_head)]
        assert abandoned[-1] == second_head
        assert first_head in abandoned

        # A further turn branches from the rewound point instead of reviving the old tip.
        third = [event async for event in app.prompt("third task")]
        third_head = third[-1].head_id

        assert (await app.session.storage.get_head()).entry_id == third_head
        assert {first_head, second_head, third_head} <= {
            entry.id for entry in (await app.session.storage.read_entries()).entries
        }
        assert _user_texts(app.session.messages) == ["first task", "third task"]


async def test_fork_copies_the_target_path_and_resumes_without_touching_the_source(
    tmp_path: Path, settings: ProviderSettings
) -> None:
    async with await CodingApplication.open(
        options(tmp_path), provider=LocalProvider(), settings=settings
    ) as app:
        await app.start()
        _ = [event async for event in app.prompt("first task")]
        _ = [event async for event in app.prompt("second task")]
        await app.command("/compact")
        third = [event async for event in app.prompt("third task")]
        target = third[-1].head_id
        entries = (await app.session.storage.read_entries()).entries
        compaction = next(entry for entry in entries if isinstance(entry, CompactionEntry))
        source = app.session.session_id
        source_ids = [entry.id for entry in entries]
        source_path = _session_path(tmp_path, source)

        result = await app.command(f"/fork {target}")

        forked = app.session.session_id
        assert forked != source
        assert forked in result.message
        copied = _stored_entries(_session_path(tmp_path, forked))
        copied_ids = {entry.id for entry in copied if not isinstance(entry, LeafEntry)}
        path_ids = {entry.id for entry in path_to_entry(entries, target)}
        expected = {
            entry.id
            for entry in entries
            if entry.id in path_ids
            or (isinstance(entry, RunCommitEntry) and entry.end_entry_id in path_ids)
        }
        # The copy is exactly the target path plus the run commits that end on it; the
        # resumed fork then appends its own startup activation after the target.
        copied_before_resume = [entry.id for entry in copied if not isinstance(entry, LeafEntry)][
            : len(expected)
        ]
        assert set(copied_before_resume) == expected
        assert copied_before_resume == [entry.id for entry in entries if entry.id in expected]
        assert any(
            isinstance(entry, CustomEntry) and entry.namespace == "run.resources"
            for entry in copied
        ), "the fork must carry the target path's resource activation"
        assert compaction.id in copied_ids
        commits = [entry for entry in copied if isinstance(entry, RunCommitEntry)]
        assert len(commits) == 3
        assert all(commit.end_entry_id in copied_ids for commit in commits)
        # The copy closes with a leaf that points the new session at the fork target.
        assert any(
            isinstance(entry, LeafEntry) and entry.parent_id == target and entry.entry_id == target
            for entry in copied
        )
        assert (await app.session.storage.get_head()).entry_id == resolve_active_leaf_id(copied)
        # The forked context resumes with the compaction summary plus the later turn.
        assert app.session.messages[0].text.startswith("Previous conversation summary:")
        assert _user_texts(app.session.messages) == ["third task"]
        # The source transcript was not rewritten by the fork.
        assert [entry.id for entry in _stored_entries(source_path)] == source_ids
        assert [
            entry.id for entry in await JsonlSessionStorage(source_path).read_all()
        ] == source_ids

    # The fork resumes on its own and the source session still resumes independently.
    async with await CodingApplication.open(
        replace(options(tmp_path), resume=forked), provider=LocalProvider(), settings=settings
    ) as reopened:
        assert reopened.session.session_id == forked
        assert (await reopened.session.storage.get_head()).entry_id == resolve_active_leaf_id(
            _stored_entries(_session_path(tmp_path, forked))
        )
        assert reopened.session.messages[0].text.startswith("Previous conversation summary:")
        assert _user_texts(reopened.session.messages) == ["third task"]

    async with await CodingApplication.open(
        replace(options(tmp_path), resume=source), provider=LocalProvider(), settings=settings
    ) as original:
        assert original.session.session_id == source
        assert (await original.session.storage.get_head()).entry_id == target
        # The compacted source keeps its summary plus the turn after it.
        assert original.session.messages[0].text.startswith("Previous conversation summary:")
        assert _user_texts(original.session.messages) == ["third task"]
