"""P2-3: one named unit of work owns every composite commit.

Plan 7.4 requires a shared UnitOfWork at the composition layer, so the session's
final events and the gateway's terminal state cannot be committed separately.
The same abstraction has to serve the second composite point, where an extension
rebind and its activation entry commit together.

These tests pin the contract rather than either call site: participants apply in
declaration order inside one write transaction, and a participant that raises
takes every earlier participant's write down with it.
"""

import sqlite3
from collections.abc import Callable

import pytest

from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.unit_of_work import CommitParticipant, UnitOfWork

Participant = Callable[[sqlite3.Connection], None]


def write_marker(marker: str) -> Participant:
    def apply(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO host_metadata(key,value_json) VALUES (?,?)", (marker, '"applied"')
        )

    return apply


def failing(connection: sqlite3.Connection) -> None:
    raise OSError("participant failure")


async def markers(database: SqliteDatabase) -> list[str]:
    return await database.run(
        lambda connection: [
            row[0] for row in connection.execute("SELECT key FROM host_metadata ORDER BY key")
        ]
    )


async def test_participants_apply_in_declaration_order(tmp_path) -> None:
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        unit = UnitOfWork(database)
        await unit.commit(
            CommitParticipant("first", write_marker("first")),
            CommitParticipant("second", write_marker("second")),
        )
        assert await markers(database) == ["first", "second"]


async def test_a_failing_participant_rolls_back_the_earlier_ones(tmp_path) -> None:
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        unit = UnitOfWork(database)
        with pytest.raises(OSError, match="participant failure"):
            await unit.commit(
                CommitParticipant("committed", write_marker("committed")),
                CommitParticipant("fails", failing),
            )
        assert await markers(database) == []


async def test_a_rerun_after_a_failure_commits_cleanly(tmp_path) -> None:
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        unit = UnitOfWork(database)
        with pytest.raises(OSError):
            await unit.commit(
                CommitParticipant("committed", write_marker("committed")),
                CommitParticipant("fails", failing),
            )
        await unit.commit(CommitParticipant("retry", write_marker("retry")))
        assert await markers(database) == ["retry"]


async def test_an_empty_commit_is_a_no_op(tmp_path) -> None:
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        unit = UnitOfWork(database)
        await unit.commit()
        assert await markers(database) == []
