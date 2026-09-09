"""Cancellation must not silently unbound accepted database work."""

import asyncio
import threading

import pytest

from run_agent_coding.storage.sqlite import DatabaseClosed, SqliteDatabase


async def test_cancelled_caller_holds_admission_until_commit(tmp_path):
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3", max_pending=1) as database:

        def slow(connection):
            started.set()
            assert release.wait(5)
            connection.execute("INSERT INTO projects VALUES ('first', '/first', 0)")

        task = asyncio.create_task(database.run(slow, write=True))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = asyncio.create_task(database.run(lambda connection: second_started.set()))
        try:
            await asyncio.sleep(0)
            assert not second.done() and not second_started.is_set()
        finally:
            release.set()
        await second
        assert second_started.is_set()
        assert (
            await database.run(
                lambda connection: connection.execute("SELECT count(*) FROM projects").fetchone()[0]
            )
            == 1
        )


async def test_close_drains_admitted_work_and_rejects_new_requests(tmp_path):
    started = threading.Event()
    release = threading.Event()
    database = await SqliteDatabase.open(tmp_path / "state.sqlite3", max_pending=1)

    def slow(connection):
        started.set()
        assert release.wait(5)
        return 42

    task = asyncio.create_task(database.run(slow))
    assert await asyncio.to_thread(started.wait, 5)
    closing = asyncio.create_task(database.aclose())
    await asyncio.sleep(0)
    try:
        with pytest.raises(DatabaseClosed):
            await database.run(lambda connection: None)
        assert not closing.done()
    finally:
        release.set()
    assert await task == 42
    await closing
    await database.aclose()


async def test_transaction_cannot_await_external_code(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:

        async def invalid(connection):
            await asyncio.sleep(0)

        with pytest.raises(TypeError, match="synchronous"):
            await database.run(invalid, write=True)
        assert (
            await database.run(lambda connection: connection.execute("SELECT 1").fetchone()[0]) == 1
        )
