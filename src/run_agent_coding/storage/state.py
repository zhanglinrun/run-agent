"""Source-bound namespaces with atomic state and resource head changes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from time import time
from uuid import uuid4

from run_agent_coding.host.contracts import ExtensionToken, HeadChange, StateChange, StateValue
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.session.contracts import SessionConflict


class ExtensionRetired(SessionConflict):
    """This source instance has been unloaded, replaced or closed."""


def assert_extension(connection: sqlite3.Connection, token: ExtensionToken) -> None:
    row = connection.execute(
        """SELECT e.*, s.owner_id AS host_owner, s.owner_active, s.owner_expires_at
           FROM extension_owners e JOIN sessions s ON e.session_id=s.session_id
           WHERE e.session_id=? AND e.source_id=?""",
        (token.session_id, token.source_id),
    ).fetchone()
    if (
        row is None
        or not row["active"]
        or row["owner_id"] != token.owner_id
        or row["generation"] != token.generation
        or row["host_owner"] != token.owner_id
        or not row["owner_active"]
        or row["owner_expires_at"] <= time()
    ):
        raise ExtensionRetired(f"Extension instance is no longer active: {token.source_id}")


async def activate_extension(database: SqliteDatabase, token: ExtensionToken) -> None:
    """Host-only binding, reusing the extension runtime's existing generation."""
    if token.generation < 0 or not token.owner_id or not token.source_id:
        raise ValueError("Invalid extension owner")

    def activate(connection: sqlite3.Connection) -> None:
        host = connection.execute(
            "SELECT owner_id, owner_active, owner_expires_at FROM sessions WHERE session_id=?",
            (token.session_id,),
        ).fetchone()
        if (
            host is None
            or host["owner_id"] != token.owner_id
            or not host["owner_active"]
            or host["owner_expires_at"] <= time()
        ):
            raise ExtensionRetired("Only the active session host can bind an extension")
        previous = connection.execute(
            "SELECT * FROM extension_owners WHERE session_id=? AND source_id=?",
            (token.session_id, token.source_id),
        ).fetchone()
        if (
            previous is not None
            and previous["owner_id"] == token.owner_id
            and (
                token.generation < previous["generation"]
                or (token.generation == previous["generation"] and not previous["active"])
            )
        ):
            raise ExtensionRetired("A retired generation cannot be reactivated")
        connection.execute(
            """INSERT INTO extension_owners VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(session_id, source_id) DO UPDATE SET
               owner_id=excluded.owner_id, generation=excluded.generation, active=1""",
            (token.session_id, token.source_id, token.owner_id, token.generation),
        )

    await database.run(activate, write=True)


async def retire_extension(database: SqliteDatabase, token: ExtensionToken) -> None:
    def retire(connection: sqlite3.Connection) -> None:
        # A disposer arriving after reload must never deactivate its replacement.
        connection.execute(
            """UPDATE extension_owners SET active=0 WHERE session_id=? AND source_id=?
               AND owner_id=? AND generation=?""",
            (token.session_id, token.source_id, token.owner_id, token.generation),
        )

    await database.run(retire, write=True)


class NamespaceState:
    """The host supplies scope and source; model tool arguments cannot switch them."""

    def __init__(self, database: SqliteDatabase, token: ExtensionToken, scope: str) -> None:
        self.database = database
        self.token = token
        self.scope = scope

    async def get(self, key: str) -> StateValue | None:
        def get(connection: sqlite3.Connection) -> StateValue | None:
            assert_extension(connection, self.token)
            row = connection.execute(
                "SELECT * FROM extension_state WHERE source_id=? AND scope=? AND key=?",
                (self.token.source_id, self.scope, key),
            ).fetchone()
            return (
                None
                if row is None
                else StateValue(key, row["version"], json.loads(row["value_json"]))
            )

        return await self.database.run(get)

    async def list(self, *, prefix: str = "", limit: int = 100) -> list[StateValue]:
        if not 1 <= limit <= 1000:
            raise ValueError("State page size must be between 1 and 1000")

        def read(connection: sqlite3.Connection) -> list[StateValue]:
            assert_extension(connection, self.token)
            rows = connection.execute(
                """SELECT * FROM extension_state WHERE source_id=? AND scope=?
                   AND substr(key, 1, ?)=? ORDER BY key LIMIT ?""",
                (self.token.source_id, self.scope, len(prefix), prefix, limit),
            ).fetchall()
            return [
                StateValue(row["key"], row["version"], json.loads(row["value_json"]))
                for row in rows
            ]

        return await self.database.run(read)

    async def compare_and_set(self, change: StateChange) -> StateValue:
        frozen = StateChange(
            change.key, change.expected_version, json.loads(canonical_json(change.value))
        )
        await self.apply_batch(states=[frozen])
        return StateValue(frozen.key, frozen.expected_version + 1, frozen.value)

    async def apply_batch(
        self, states: Sequence[StateChange] = (), heads: Sequence[HeadChange] = ()
    ) -> None:
        if len({change.key for change in states}) != len(states) or len(
            {change.key for change in heads}
        ) != len(heads):
            raise ValueError("Duplicate keys in atomic namespace changes")
        # A frozen copy prevents mutation while this operation waits for admission.
        frozen_states = json.loads(canonical_json([asdict(change) for change in states]))
        frozen_heads = json.loads(canonical_json([asdict(change) for change in heads]))

        def apply(connection: sqlite3.Connection) -> None:
            assert_extension(connection, self.token)
            for change in frozen_states:
                row = connection.execute(
                    "SELECT version FROM extension_state WHERE source_id=? AND scope=? AND key=?",
                    (self.token.source_id, self.scope, change["key"]),
                ).fetchone()
                version = row[0] if row else 0
                if version != change["expected_version"]:
                    raise SessionConflict(f"State version changed: {change['key']}")
                connection.execute(
                    """INSERT INTO extension_state VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(source_id, scope, key) DO UPDATE SET
                       version=excluded.version, value_json=excluded.value_json""",
                    (
                        self.token.source_id,
                        self.scope,
                        change["key"],
                        version + 1,
                        canonical_json(change["value"]),
                    ),
                )
            for change in frozen_heads:
                row = connection.execute(
                    "SELECT head_version FROM resources "
                    "WHERE source_id=? AND scope=? AND resource_key=?",
                    (self.token.source_id, self.scope, change["key"]),
                ).fetchone()
                if row is None or row[0] != change["expected_version"]:
                    raise SessionConflict(f"Resource head changed: {change['key']}")
                exists = connection.execute(
                    """SELECT 1 FROM resource_versions WHERE source_id=? AND scope=?
                       AND resource_key=? AND version=?""",
                    (self.token.source_id, self.scope, change["key"], change["version"]),
                ).fetchone()
                if exists is None:
                    raise KeyError("Publication must point to an existing immutable version")
                connection.execute(
                    "UPDATE resources SET head_version=? "
                    "WHERE source_id=? AND scope=? AND resource_key=?",
                    (change["version"], self.token.source_id, self.scope, change["key"]),
                )
                connection.execute(
                    "INSERT INTO resource_publications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid4().hex,
                        self.token.source_id,
                        self.scope,
                        change["key"],
                        change["expected_version"],
                        change["version"],
                        change["reason"],
                        canonical_json(change["evidence"]),
                        self.token.session_id,
                        self.token.owner_id,
                        self.token.generation,
                        time(),
                    ),
                )

        await self.database.run(apply, write=True)
