"""Behavioral evidence for transactional history, fences and branch recovery."""

import asyncio
import sqlite3
import subprocess
import sys

import pytest

from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SchemaMismatch, SqliteDatabase
from run_agent_core.messages import UserMessage
from run_agent_core.session.contracts import SessionConflict, StaleRunToken
from run_agent_core.session.entries import MessageEntry


def message(identity, parent=None, text=None):
    return MessageEntry(
        id=identity,
        parent_id=parent,
        timestamp=1,
        message=UserMessage(content=text or identity, timestamp=1),
    )


@pytest.fixture
async def store(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        repository = SqliteSessionRepository(database)
        await repository.create_session(
            cwd=tmp_path, principal_id="local", model="test", session_id="s"
        )
        token = await repository.claim("s", owner_id="host", run_id="run")
        yield database, repository, token


async def test_cold_start_persistence_reopen_and_fork(tmp_path):
    path = tmp_path / "fresh" / "state.sqlite3"
    async with await SqliteDatabase.open(path) as database:
        repository = SqliteSessionRepository(database)
        record = await repository.create_session(
            cwd=tmp_path, principal_id="alice", model="model", session_id="s"
        )
        other = await repository.create_session(cwd=tmp_path, principal_id="bob", model="model")
        assert other.project_id == record.project_id
        token = await repository.claim("s", owner_id="host1", run_id="run1")
        await repository.append_entries(
            [message("a"), message("b", "a")], token=token, expected_head=None
        )
        await repository.release(token)
    async with await SqliteDatabase.open(path) as database:
        repository = SqliteSessionRepository(database)
        token = await repository.claim("s", owner_id="host2", run_id="run2")
        assert token.generation == 2
        assert [
            record.session_id for record in await repository.list_sessions(principal_id="alice")
        ] == ["s"]
        await repository.fork_branch(token=token, branch_id="alternative", at_entry_id="a")
        await repository.append_entries(
            [message("c", "a")], token=token, branch_id="alternative", expected_head="a"
        )
        assert [
            item.id for item in (await repository.read_entries("s", branch_id="main")).entries
        ] == ["a", "b"]
        assert [
            item.id
            for item in (await repository.read_entries("s", branch_id="alternative")).entries
        ] == ["a", "c"]
        assert (await repository.get_head("s")).entry_id == "b"
        assert (
            await database.run(
                lambda connection: connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            == "ok"
        )
        assert (
            await database.run(
                lambda connection: connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            == []
        )


@pytest.mark.parametrize("point", ["entry_inserted", "head_updated"])
async def test_fault_rolls_back_all_events_sequence_and_head(store, point):
    database, repository, token = store

    def fault(name):
        if name == point:
            raise RuntimeError("injected failure")

    repository.fault = fault
    entries = [message("a"), message("b", "a")]
    with pytest.raises(RuntimeError, match="injected failure"):
        await repository.append_entries(entries, token=token, expected_head=None)
    assert (await repository.read_entries("s")).entries == ()
    assert (await repository.get_head("s")).entry_id is None
    assert entries[0].seq is None
    repository.fault = None
    receipt = await repository.append_entries(entries, token=token, expected_head=None)
    assert receipt.sequences == (1, 2)


async def test_idempotent_retry_does_not_rewind_head_or_duplicate(store):
    _, repository, token = store
    first = [message("a"), message("b", "a")]
    await repository.append_entries(first, token=token, expected_head=None)
    await repository.append_entries([message("c", "b")], token=token, expected_head="b")
    receipt = await repository.append_entries(first, token=token, expected_head=None)
    assert receipt.applied is False and receipt.sequences == (1, 2)
    assert (await repository.get_head("s")).entry_id == "c"
    assert len((await repository.read_entries("s")).entries) == 3
    with pytest.raises(SessionConflict, match="different content"):
        await repository.append_entries(
            [message("a", text="changed")], token=token, expected_head=None
        )


async def test_expected_head_allows_only_one_concurrent_writer(store):
    _, repository, token = store
    results = await asyncio.gather(
        repository.append_entries([message("a")], token=token, expected_head=None),
        repository.append_entries([message("b")], token=token, expected_head=None),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SessionConflict) for result in results) == 1
    assert len((await repository.read_entries("s")).entries) == 1


async def test_ownership_fence_survives_release_and_takeover(store):
    _, repository, token = store
    with pytest.raises(SessionConflict):
        await repository.claim("s", owner_id="other", run_id="other")
    new = await repository.claim("s", owner_id="new-host", run_id="new-run", takeover=True)
    with pytest.raises(StaleRunToken):
        await repository.append_entries([message("late")], token=token, expected_head=None)
    with pytest.raises(StaleRunToken):
        await repository.release(token)
    await repository.append_entries([message("new")], token=new, expected_head=None)
    await repository.release(new)
    with pytest.raises(StaleRunToken):
        await repository.append_entries([message("new")], token=new, expected_head=None)
    newest = await repository.claim("s", owner_id="new-host", run_id="next-run")
    assert newest.generation > new.generation


async def test_expired_owner_cannot_renew_or_write(store):
    _, repository, token = store
    await repository.release(token)
    clock = [100.0]
    repository.clock = lambda: clock[0]
    current = await repository.claim("s", owner_id="owner", run_id="run", ttl_seconds=10)
    clock[0] = 111.0
    with pytest.raises(StaleRunToken):
        await repository.renew(current)
    with pytest.raises(StaleRunToken):
        await repository.append_entries([message("late")], token=current, expected_head=None)
    recovered = await repository.claim("s", owner_id="recovery", run_id="new")
    assert recovered.generation > current.generation


async def test_branch_pagination_watermark_and_wrong_ancestor(store):
    _, repository, token = store
    await repository.append_entries(
        [message("a"), message("b", "a"), message("c", "b")], token=token, expected_head=None
    )
    await repository.fork_branch(token=token, branch_id="fork", at_entry_id="a")
    await repository.append_entries(
        [message("d", "a")], token=token, branch_id="fork", expected_head="a"
    )
    page = await repository.read_entries("s", branch_id="fork", limit=1)
    assert [item.id for item in page.entries] == ["a"] and page.next_seq == 1
    page2 = await repository.read_entries("s", branch_id="fork", after_seq=page.next_seq, limit=1)
    assert [item.id for item in page2.entries] == ["d"] and page2.next_seq is None
    assert [
        item.id
        for item in (await repository.read_entries("s", branch_id="fork", through_seq=2)).entries
    ] == ["a"]
    with pytest.raises(SessionConflict, match="ancestor"):
        await repository.fork_branch(
            token=token, branch_id="bad", source_branch_id="fork", at_entry_id="b"
        )


async def test_snapshot_is_bound_to_committed_history(store):
    database, repository, token = store
    await repository.append_entries([message("a")], token=token, expected_head=None)
    payload = {"messages": ["a"], "resources": {"skill": "frozen-v1"}, "tools": []}
    snapshot = await repository.put_snapshot(
        token=token, branch_id="main", expected_head="a", builder_version="1", payload=payload
    )
    await repository.append_entries([message("b", "a")], token=token, expected_head="a")
    value = await repository.get_snapshot(snapshot)
    assert value["watermark"] == 1 and value["payload"] == payload
    tail = await repository.read_entries("s", branch_id="main", after_seq=value["watermark"])
    assert [item.id for item in tail.entries] == ["b"]
    with pytest.raises(SessionConflict):
        await repository.put_snapshot(
            token=token, branch_id="main", expected_head="a", builder_version="1", payload=payload
        )
    await database.run(
        lambda connection: connection.execute(
            "UPDATE context_snapshots SET payload_json='{}' WHERE snapshot_id=?", (snapshot,)
        ),
        write=True,
    )
    with pytest.raises(SessionConflict, match="hash mismatch"):
        await repository.get_snapshot(snapshot)


async def test_unknown_schema_rejected_without_conversion(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=99")
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('original')")
    with pytest.raises(SchemaMismatch):
        await SqliteDatabase.open(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT value FROM preserved").fetchone()[0] == "original"


async def test_two_hosts_serialize_schema_initialization(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    one, two = await asyncio.gather(SqliteDatabase.open(path), SqliteDatabase.open(path))
    try:
        assert (
            await one.run(
                lambda connection: connection.execute("PRAGMA user_version").fetchone()[0]
            )
            == 1
        )
        assert (
            await two.run(
                lambda connection: connection.execute("PRAGMA user_version").fetchone()[0]
            )
            == 1
        )
    finally:
        await asyncio.gather(one.aclose(), two.aclose())


async def test_process_death_inside_transaction_leaves_no_partial_head(tmp_path):
    path = tmp_path / "crash.sqlite3"
    code = """
import asyncio, os, sys
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_core.session.entries import MessageEntry
from run_agent_core.messages import UserMessage
async def run():
    db = await SqliteDatabase.open(sys.argv[1])
    repo = SqliteSessionRepository(db)
    await repo.create_session(cwd='.', principal_id='local', model='test', session_id='s')
    token = await repo.claim('s', owner_id='dead-host', run_id='r')
    repo.fault = lambda point: os._exit(77) if point == 'head_updated' else None
    await repo.append_entries([MessageEntry(id='partial', message=UserMessage(content='x'))], token=token, expected_head=None)
asyncio.run(run())
"""
    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code, str(path)], capture_output=True, timeout=20
    )
    assert result.returncode == 77, result.stderr
    async with await SqliteDatabase.open(path) as database:
        repository = SqliteSessionRepository(database)
        assert (await repository.get_head("s")).entry_id is None
        assert (await repository.read_entries("s")).entries == ()
        token = await repository.claim("s", owner_id="recovery", run_id="r2", takeover=True)
        receipt = await repository.append_entries(
            [message("recovered")], token=token, expected_head=None
        )
        assert receipt.sequences == (1,)
