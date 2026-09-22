"""Append-only JSONL session trees: persist, atomic batch replace, resume, fork."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from run_agent_coding import session_manager as session_manager_module
from run_agent_coding.jsonl_storage import SessionWriter
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.session import _entries_for_fork
from run_agent_coding.session_manager import SessionManager
from run_agent_core.messages import AssistantMessage, TextContent, UserMessage
from run_agent_core.session.contracts import RunOutcome, SessionConflict, StaleRunToken
from run_agent_core.session.entries import (
    CompactionEntry,
    CustomEntry,
    LeafEntry,
    MessageEntry,
    RunCommitEntry,
    SessionInfoEntry,
)
from run_agent_core.session.jsonl import entry_from_json_line, entry_to_json_line
from run_agent_core.session.storage import JsonlSessionStorage, StorageDiagnostics
from run_agent_core.session.tree import SessionTree, path_to_entry, resolve_active_leaf_id


def _paths(tmp_path: Path) -> RunAgentPaths:
    return RunAgentPaths(home=tmp_path / "state", agents_home=tmp_path / "agents")


async def test_jsonl_append_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    first = MessageEntry(message=UserMessage(content="hello"))
    second = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="hi")])
    )
    await storage.append(first)
    await storage.append(second)
    reopened = JsonlSessionStorage(path)
    entries = await reopened.read_all()
    assert [entry.id for entry in entries] == [first.id, second.id]
    assert entries[0].message.text == "hello"
    assert entries[1].message.text == "hi"


async def test_append_batch_is_atomic_and_leaves_tmp_behind_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    first = CustomEntry(namespace="test", data={"n": 1})
    await storage.append(first)
    batch = (
        CustomEntry(parent_id=first.id, namespace="test", data={"n": 2}),
        CustomEntry(parent_id=first.id, namespace="test", data={"n": 3}),
    )
    import os

    original = os.replace

    def fail_replace(src: str, dst: str) -> None:
        if Path(dst) == path:
            raise OSError("injected replace failure")
        original(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        await storage.append_batch(batch)
    recovered = await JsonlSessionStorage(path).read_all()
    assert [entry.id for entry in recovered] == [first.id]


async def test_incomplete_tmp_is_discarded_on_read(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    entry = CustomEntry(namespace="test", data={"keep": True})
    await storage.append(entry)
    storage.temp_path.write_text("{not json\n", encoding="utf-8")
    rows = await storage.read_all()
    assert [item.id for item in rows] == [entry.id]
    assert not storage.temp_path.exists()


async def test_session_manager_resume_reads_jsonl(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        record = await manager.create_session(cwd=tmp_path, model="test")
        writer = await manager.open_storage(record.id)
        user = MessageEntry(message=UserMessage(content="remember me"))
        await writer.append_entries((user,), expected_head=None, token=writer.token)
        await writer.aclose()
        await manager.aclose()
    finally:
        if not manager._closed:
            await manager.aclose()
    manager = SessionManager(_paths(tmp_path))
    try:
        writer = await manager.open_storage(record.id)
        entries = await writer.read_all()
        assert entries[-1].message.text == "remember me"
        assert record.path is not None and record.path.is_file()
    finally:
        await manager.aclose()


async def test_fork_appends_a_new_timeline(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        record = await manager.create_session(cwd=tmp_path, model="test")
        writer = await manager.open_storage(record.id)
        first = CustomEntry(namespace="test", data={"n": 1})
        second = CustomEntry(parent_id=first.id, namespace="test", data={"n": 2})
        await writer.append_entries((first,), expected_head=None, token=writer.token)
        await writer.append_entries((second,), expected_head=first.id, token=writer.token)
        original = await writer.get_head()
        marker = CustomEntry(
            parent_id=first.id, namespace="run.resources", data={"reason": "branch"}
        )
        forked = await writer.fork(first.id, token=writer.token, entries=(marker,))
        assert forked.entry_id == marker.id
        assert forked.branch_id != original.branch_id
        entries = await writer.read_all()
        assert {entry.id for entry in entries} >= {first.id, second.id, marker.id}
        with pytest.raises(SessionConflict):
            await writer.fork(
                first.id,
                token=writer.token,
                entries=(CustomEntry(parent_id="missing", namespace="test", data={}),),
            )
        assert (await writer.get_head()).entry_id == marker.id
    finally:
        await manager.aclose()


async def test_retired_run_token_cannot_append(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        record = await manager.create_session(cwd=tmp_path, model="test")
        writer = await manager.open_storage(record.id)
        token = await writer.begin_run("run-one")
        entry = CustomEntry(namespace="test", data={"key": "value"})
        outcome = RunOutcome(token, writer.branch_id, "cancelled", None, (entry,))
        receipt = await writer.complete_run(outcome)
        assert await writer.complete_run(outcome) == receipt
        with pytest.raises(StaleRunToken):
            await writer.append_entries(
                (CustomEntry(parent_id=entry.id, namespace="test", data={}),),
                expected_head=entry.id,
                token=token,
            )
        with pytest.raises(SessionConflict):
            await writer.complete_run(RunOutcome(token, writer.branch_id, "cancelled", entry.id))
        assert (await writer.begin_run("run-two")).generation > token.generation
    finally:
        await manager.aclose()


async def test_session_index_is_append_only_lww_and_compacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_manager_module, "_INDEX_COMPACTION_MIN_RECORDS", 1)
    manager = SessionManager(_paths(tmp_path))
    try:
        record = await manager.create_session(
            cwd=tmp_path, model="old", title="old", session_id="index-test"
        )
        project_index = manager.project_index_path(tmp_path)
        assert len(project_index.read_text(encoding="utf-8").splitlines()) == 1
        for index in range(4):
            updated = await manager.touch_session(record.id, model="new", title=f"title-{index}")
        assert updated.model == "new"
        assert updated.title == "title-3"
        assert await manager.get_session(record.id) == updated
        assert len(project_index.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        await manager.aclose()


async def test_index_compaction_failure_keeps_the_previous_complete_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite that dies mid-flight must leave the append-only index readable."""
    monkeypatch.setattr(session_manager_module, "_INDEX_COMPACTION_MIN_RECORDS", 1)
    manager = SessionManager(_paths(tmp_path))
    try:
        record = await manager.create_session(
            cwd=tmp_path, model="old", title="old", session_id="compact-failure"
        )
        project_index = manager.project_index_path(tmp_path)
        for index in range(3):
            await manager.touch_session(record.id, model="new", title=f"title-{index}")
        # Four rows with one live record: the fourth append is the one that compacts.
        assert len(project_index.read_text(encoding="utf-8").splitlines()) == 4

        original_replace = os.replace

        def fail_replace(source: object, destination: object) -> None:
            if Path(str(destination)) == project_index:
                raise OSError("injected index replace failure")
            original_replace(source, destination)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected index replace failure"):
            await manager.touch_session(record.id, model="new", title="title-3")

        # The appended row survived; the interrupted rewrite left no partial file behind.
        assert len(project_index.read_text(encoding="utf-8").splitlines()) == 5
        assert list(tmp_path.glob(".index.jsonl.*.tmp")) == []
        recorded = manager._read_index(project_index)
        assert [(row.id, row.title) for row in recorded] == [(record.id, "title-3")]

        # Once the fault clears, the very next compaction succeeds and reduces the index.
        monkeypatch.setattr(os, "replace", original_replace)
        updated = await manager.touch_session(record.id, model="new", title="title-4")
        assert len(project_index.read_text(encoding="utf-8").splitlines()) == 1
        assert [row.title for row in manager._read_index(project_index)] == ["title-4"]
        assert (await manager.get_session(record.id)) == updated
    finally:
        await manager.aclose()


async def test_directory_fsync_failure_is_recorded_and_the_state_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot fsync a directory: the write continues, the failure is visible."""
    diagnostics = StorageDiagnostics()
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path, diagnostics=diagnostics)
    info = SessionInfoEntry(cwd=str(tmp_path))
    first = MessageEntry(parent_id=info.id, message=UserMessage(content="one"))
    await storage.append(info)
    await storage.append(first)
    assert diagnostics.directory_fsync_failures == 0

    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    second = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="two")])
    )
    await storage.append_batch((second,))

    assert diagnostics.directory_fsync_attempts >= 1
    assert diagnostics.directory_fsync_failures >= 1
    assert diagnostics.last_directory_fsync_error is not None
    assert diagnostics.last_directory_fsync_path == str(tmp_path)

    # The committed batch is intact even though the directory flush did not happen.
    writer = SessionWriter(JsonlSessionStorage(path, diagnostics=diagnostics), "session")
    entries = await writer.read_all()
    assert [entry.id for entry in entries] == [info.id, first.id, second.id]
    assert [entry.seq for entry in entries] == [1, 2, 3]
    assert resolve_active_leaf_id(entries) == second.id
    assert (await writer.get_head()).entry_id == second.id

    # A later write still succeeds: the failure is recorded, not sticky.
    third = MessageEntry(
        parent_id=second.id, message=AssistantMessage(content=[TextContent(text="three")])
    )
    await storage.append_batch((third,))
    assert [entry.id for entry in await storage.read_all()] == [
        info.id,
        first.id,
        second.id,
        third.id,
    ]


def test_fork_path_keeps_resources_compaction_and_run_commit() -> None:
    resource = CustomEntry(namespace="run.resources", data={})
    message = MessageEntry(parent_id=resource.id, message=UserMessage(content="completed run"))
    commit = RunCommitEntry(
        parent_id=message.id,
        run_id="run-one",
        branch_id="main",
        status="succeeded",
        start_entry_id=resource.id,
        end_entry_id=message.id,
    )
    compaction = CompactionEntry(
        parent_id=message.id, summary="summary", replaces_entry_ids=[message.id]
    )
    abandoned = CustomEntry(parent_id=message.id, namespace="abandoned", data={})
    entries = (resource, message, commit, compaction, abandoned)

    copied = _entries_for_fork(entries, path_to_entry(list(entries), compaction.id))

    assert [entry.id for entry in copied] == [
        resource.id,
        message.id,
        commit.id,
        compaction.id,
    ]


def test_jsonl_round_trip_preserves_entry_identity() -> None:
    entry = MessageEntry(message=UserMessage(content="wire"))
    decoded = entry_from_json_line(entry_to_json_line(entry))
    assert decoded.id == entry.id
    assert decoded.message.text == "wire"


def test_leaf_entry_round_trip_uses_entry_id() -> None:
    pointer = LeafEntry(parent_id="msg", entry_id="msg")
    decoded = entry_from_json_line(entry_to_json_line(pointer))
    assert isinstance(decoded, LeafEntry)
    assert decoded.entry_id == "msg"
    assert '"entryId"' in entry_to_json_line(pointer)
    first = MessageEntry(id="first", message=UserMessage(content="a"))
    second = MessageEntry(id="second", parent_id=first.id, message=UserMessage(content="b"))
    stale = LeafEntry(parent_id=first.id, entry_id=first.id)
    latest = LeafEntry(parent_id=second.id, entry_id=second.id)
    assert resolve_active_leaf_id((first, second, stale, latest)) == second.id


async def test_single_append_does_not_replace_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    info = SessionInfoEntry(cwd=str(tmp_path))
    first = MessageEntry(parent_id=info.id, message=UserMessage(content="keep"))
    await storage.append(info)
    prefix = path.read_bytes()
    await storage.append(first)

    import os

    def fail_replace(src: str, dst: str) -> None:
        raise AssertionError("single append must not os.replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    third = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="ok")])
    )
    await storage.append(third)
    recovered = await JsonlSessionStorage(path).read_all()
    assert [entry.id for entry in recovered] == [info.id, first.id, third.id]
    assert path.read_bytes().startswith(prefix)


async def test_rewind_persists_leaf_entry_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    storage = JsonlSessionStorage(path)
    info = SessionInfoEntry(cwd=str(tmp_path))
    first = MessageEntry(parent_id=info.id, message=UserMessage(content="root"))
    second = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="leaf")])
    )
    await storage.append(info)
    await storage.append(first)
    await storage.append(second)
    pointer = LeafEntry(parent_id=first.id, entry_id=first.id)
    await storage.append(pointer)
    reopened = await JsonlSessionStorage(path).read_all()
    assert resolve_active_leaf_id(reopened) == first.id
    assert [entry.id for entry in reopened] == [info.id, first.id, second.id, pointer.id]


async def test_legacy_session_info_current_id_still_resumes(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    info = SessionInfoEntry(cwd=str(tmp_path), current_id="first")
    first = MessageEntry(id="first", parent_id=info.id, message=UserMessage(content="root"))
    second = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="later")])
    )
    path.write_text(
        entry_to_json_line(info) + entry_to_json_line(first) + entry_to_json_line(second),
        encoding="utf-8",
    )
    reopened = await JsonlSessionStorage(path).read_all()
    assert resolve_active_leaf_id(reopened) == first.id


async def test_fork_copies_path_into_a_new_workspace_session(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        source = await manager.create_session(cwd=tmp_path, model="test")
        assert source.path == tmp_path / ".run" / "sessions" / f"{source.id}.jsonl"
        writer = await manager.open_storage(source.id)
        info = SessionInfoEntry(cwd=str(tmp_path))
        first = MessageEntry(parent_id=info.id, message=UserMessage(content="keep"))
        second = MessageEntry(
            parent_id=first.id, message=AssistantMessage(content=[TextContent(text="drop")])
        )
        await writer.append_entries((info,), expected_head=None, token=writer.token)
        await writer.append_entries((first,), expected_head=info.id, token=writer.token)
        await writer.append_entries((second,), expected_head=first.id, token=writer.token)
        await writer.aclose()
        forked = await manager.fork_session(
            cwd=tmp_path,
            model="test",
            provider_name=None,
            title=None,
            entries=(info, first),
            current_id=first.id,
        )
        assert forked.id != source.id
        assert forked.path == tmp_path / ".run" / "sessions" / f"{forked.id}.jsonl"
        copied = await JsonlSessionStorage(forked.path).read_all()
        assert [entry.id for entry in copied[:2]] == [info.id, first.id]
        assert isinstance(copied[-1], LeafEntry)
        assert resolve_active_leaf_id(copied) == first.id
        original = await JsonlSessionStorage(source.path).read_all()
        assert [entry.id for entry in original] == [info.id, first.id, second.id]
        located = await manager.get_session(forked.id)
        assert located is not None
    finally:
        await manager.aclose()


def test_session_tree_rewind_keeps_abandoned_branch() -> None:
    first = MessageEntry(message=UserMessage(content="a"))
    second = MessageEntry(
        parent_id=first.id, message=AssistantMessage(content=[TextContent(text="b")])
    )
    tree = SessionTree((first, second))
    tree.rewind(first.id)
    assert tree.current_id == first.id
    assert second.id in tree.entries
    assert [entry.id for entry in tree.path()] == [first.id]
