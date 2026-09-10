"""Required process lifecycle journal; optional traces do not authorize recovery."""

from __future__ import annotations

import sqlite3
from time import time

from run_agent_coding.storage.sessions import SqliteSessionRepository, canonical_json
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.session.contracts import RunToken, SessionConflict
from run_agent_core.types import JSONValue


class SqliteProcessJournal:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    async def record(self, token: RunToken, payload: dict[str, JSONValue]) -> None:
        identity, phase = payload["process_id"], payload["phase"]
        body = canonical_json(payload)

        def record(connection: sqlite3.Connection) -> None:
            if phase == "launching":
                SqliteSessionRepository(self.database).assert_token(connection, token)
                connection.execute(
                    "INSERT INTO managed_processes(process_id,session_id,run_id,owner_id,"
                    "generation,status,intent_json,updated_at) VALUES (?,?,?,?,?,'launching',?,?)",
                    (
                        identity,
                        token.session_id,
                        token.run_id,
                        token.owner_id,
                        token.generation,
                        body,
                        time(),
                    ),
                )
                return
            row = connection.execute(
                "SELECT * FROM managed_processes WHERE process_id=?", (identity,)
            ).fetchone()
            if row is None or (
                row["session_id"],
                row["run_id"],
                row["owner_id"],
                row["generation"],
            ) != (token.session_id, token.run_id, token.owner_id, token.generation):
                raise SessionConflict("Process lifecycle does not belong to its writer")
            if phase == "started":
                SqliteSessionRepository(self.database).assert_token(connection, token)
                if row["status"] != "launching":
                    raise SessionConflict("Process launch already recorded")
                connection.execute(
                    "UPDATE managed_processes SET status='running',native_json=?,updated_at=? "
                    "WHERE process_id=?",
                    (body, time(), identity),
                )
            elif phase == "exited":
                events = payload.get("events")
                if not isinstance(events, list) or "empty" not in events:
                    raise ValueError("Process completion requires verified empty membership")
                if row["status"] == "exited" and row["outcome_json"] != body:
                    raise SessionConflict("Process exit has a different outcome")
                connection.execute(
                    "UPDATE managed_processes SET status='exited',outcome_json=?,updated_at=? "
                    "WHERE process_id=?",
                    (body, time(), identity),
                )
            elif phase == "launch_failed":
                if row["status"] != "launching":
                    raise SessionConflict("Running process cannot be reported as a failed launch")
                connection.execute(
                    "UPDATE managed_processes SET status='launch_failed',"
                    "outcome_json=?,updated_at=? WHERE process_id=?",
                    (body, time(), identity),
                )
            else:
                raise ValueError("Unknown process lifecycle phase")

        await self.database.run(record, write=True)
