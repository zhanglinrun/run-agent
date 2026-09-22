"""Durable run boundaries: ``begin_run``/``complete_run`` and ``read_completed_run``.

Moved out of ``test_jsonl_sessions.py`` and extended: a completed run must be readable
by run id, must expose only its own activity on the active branch, and must survive a
process restart. The exact failure semantics live in
``run_agent_coding.storage.host.WriterHistory.read_completed_run``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from run_agent_coding.jsonl_storage import SessionWriter
from run_agent_coding.storage.host import WriterHistory
from run_agent_core.session.contracts import RunOutcome
from run_agent_core.session.entries import CustomEntry, LeafEntry, RunCommitEntry
from run_agent_core.session.storage import JsonlSessionStorage
from run_agent_core.session.tree import resolve_active_leaf_id


def _history(path: Path) -> WriterHistory:
    """A history handle over a fresh writer, as a restarted host would build it."""
    return WriterHistory(SessionWriter(JsonlSessionStorage(path), "session"), lambda: None)


async def test_begin_run_complete_run_records_boundaries_and_leaf(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    writer = SessionWriter(JsonlSessionStorage(path), "session")
    before = CustomEntry(namespace="test", data={"phase": "before"})
    await writer.append_entries((before,), expected_head=None, token=writer.token)

    token = await writer.begin_run("run-one")
    during = CustomEntry(parent_id=before.id, namespace="test", data={"phase": "during"})
    await writer.append_entries((during,), expected_head=before.id, token=token)
    receipt = await writer.complete_run(RunOutcome(token, writer.branch_id, "succeeded", during.id))

    entries = await writer.read_all()
    commit = next(entry for entry in entries if isinstance(entry, RunCommitEntry))
    leaf = next(entry for entry in entries if isinstance(entry, LeafEntry))
    assert commit.run_id == "run-one"
    assert commit.branch_id == writer.branch_id
    assert commit.status == "succeeded"
    assert commit.start_entry_id == before.id
    assert commit.end_entry_id == during.id
    assert receipt.head_id == during.id
    # The commit and the leaf pointer land after the run's own entries.
    assert [entry.id for entry in entries] == [before.id, during.id, commit.id, leaf.id]
    assert leaf.entry_id == during.id
    assert resolve_active_leaf_id(entries) == during.id


async def test_read_completed_run_keeps_a_later_run_out_of_an_earlier_one(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    writer = SessionWriter(JsonlSessionStorage(path), "session")
    before = CustomEntry(namespace="test", data={"phase": "before"})
    await writer.append_entries((before,), expected_head=None, token=writer.token)

    token_a = await writer.begin_run("run-a")
    during_a = CustomEntry(parent_id=before.id, namespace="test", data={"phase": "a"})
    await writer.append_entries((during_a,), expected_head=before.id, token=token_a)
    await writer.complete_run(RunOutcome(token_a, writer.branch_id, "succeeded", during_a.id))

    # Run B starts from run A's leaf, so its entries hang off run A's end entry.
    token_b = await writer.begin_run("run-b")
    during_b = CustomEntry(parent_id=during_a.id, namespace="test", data={"phase": "b"})
    # A bare entry does not move the durable leaf, so the tip pointer is appended too.
    await writer.append_entries(
        (during_b, LeafEntry(parent_id=during_b.id, entry_id=during_b.id)),
        expected_head=during_a.id,
        token=token_b,
    )
    await writer.complete_run(RunOutcome(token_b, writer.branch_id, "succeeded", during_b.id))

    entries = await writer.read_all()
    commits = {entry.run_id: entry for entry in entries if isinstance(entry, RunCommitEntry)}
    assert set(commits) == {"run-a", "run-b"}
    assert (commits["run-a"].start_entry_id, commits["run-a"].end_entry_id) == (
        before.id,
        during_a.id,
    )
    assert (commits["run-b"].start_entry_id, commits["run-b"].end_entry_id) == (
        during_a.id,
        during_b.id,
    )

    history = _history(path)
    # Each run reads back only its own activity: run A excludes run B and the
    # pre-run entry, run B excludes everything before its start boundary.
    assert [entry.id for entry in await history.read_completed_run("run-a")] == [during_a.id]
    assert [entry.id for entry in await history.read_completed_run("run-b")] == [during_b.id]


async def test_read_completed_run_rejects_unknown_and_incomplete_runs(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    writer = SessionWriter(JsonlSessionStorage(path), "session")
    before = CustomEntry(namespace="test", data={"phase": "before"})
    await writer.append_entries((before,), expected_head=None, token=writer.token)
    token = await writer.begin_run("run-open")
    await writer.append_entries(
        (CustomEntry(parent_id=before.id, namespace="test", data={"phase": "open"}),),
        expected_head=before.id,
        token=token,
    )

    history = _history(path)
    with pytest.raises(KeyError, match="Unknown or incomplete run"):
        await history.read_completed_run("missing")
    with pytest.raises(KeyError, match="Unknown or incomplete run"):
        await history.read_completed_run("run-open")  # begun but never committed


async def test_a_committed_run_survives_a_process_restart(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    writer = SessionWriter(JsonlSessionStorage(path), "session")
    token = await writer.begin_run("run-one")
    during = CustomEntry(namespace="test", data={"phase": "during"})
    await writer.append_entries((during,), expected_head=None, token=token)
    await writer.complete_run(RunOutcome(token, writer.branch_id, "succeeded", during.id))

    # A brand-new storage instance sees the same committed run; the old writer is gone.
    reopened = SessionWriter(JsonlSessionStorage(path), "session")
    history = WriterHistory(reopened, lambda: None)
    assert [entry.id for entry in await history.read_completed_run("run-one")] == [during.id]
    assert (await reopened.get_head()).entry_id == during.id
