"""Durable delivery obligations for final replies.

A reply that the model already produced but the platform has not confirmed is the one
thing the gateway can lose without a trace. The ledger writes three checkpoints around a
send into a small SQLite file: ``pending`` before the first attempt, ``attempting`` right
before the await, then ``delivered`` or ``failed``. On startup the rows whose owning process
is dead are handed back for redelivery. A ``pending`` row is resent plainly; ``attempting``
and ``failed`` rows carry a visible recovered-reply marker, because the platform may already
have the message and a silent duplicate would be worse than an honest one.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from run_agent_coding.host.process_identity import process_identity

ObligationState = Literal["pending", "attempting", "delivered", "failed", "unknown"]
RECOVERED_MARKER = "（以下是网关重启前未确认送达的回复，可能与之前的消息重复。）\n\n"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_obligations (
    obligation_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    reply_to TEXT,
    thread_id TEXT,
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','attempting','delivered','failed','unknown')),
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 1,
    error TEXT NOT NULL DEFAULT '',
    owner_pid INTEGER NOT NULL,
    owner_identity TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_obligations_state ON delivery_obligations(state, updated_at);
"""


@dataclass(frozen=True, slots=True)
class Obligation:
    obligation_id: str
    session_key: str
    chat_id: str
    reply_to: str | None
    thread_id: str | None
    content: str
    state: ObligationState
    error: str = ""
    chunk_index: int = 0
    chunk_count: int = 1

    @property
    def recovered_content(self) -> str:
        """The text to resend: marked when the first attempt may have landed."""
        if self.state == "pending":
            return self.content
        return RECOVERED_MARKER + self.content


class DeliveryLedger:
    def __init__(self, path: Path, *, retention_seconds: float = 7 * 24 * 3600) -> None:
        self.path = path
        self.retention_seconds = retention_seconds
        self._pid = os.getpid()
        self._identity = process_identity(self._pid) or f"pid:{self._pid}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(delivery_obligations)").fetchall()
            }
            if "chunk_index" not in columns:
                connection.execute(
                    "ALTER TABLE delivery_obligations RENAME TO delivery_obligations_legacy"
                )
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO delivery_obligations "
                    "(obligation_id,session_key,chat_id,reply_to,thread_id,content,state,error,"
                    "owner_pid,owner_identity,created_at,updated_at) "
                    "SELECT obligation_id,session_key,chat_id,reply_to,thread_id,"
                    "content,state,error,owner_pid,owner_identity,created_at,updated_at "
                    "FROM delivery_obligations_legacy"
                )
                connection.execute("DROP TABLE delivery_obligations_legacy")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS delivery_obligations_state "
                    "ON delivery_obligations(state, updated_at)"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def obligation_id(
        session_key: str,
        reply_to: str | None,
        content: str,
        *,
        chunk_index: int = 0,
        chunk_count: int = 1,
    ) -> str:
        digest = hashlib.sha256(
            f"{session_key}\0{reply_to or ''}\0{chunk_index}/{chunk_count}\0{content}".encode()
        ).hexdigest()
        return digest[:32]

    def record(
        self,
        session_key: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        chunk_index: int = 0,
        chunk_count: int = 1,
    ) -> Obligation:
        obligation = Obligation(
            self.obligation_id(
                session_key,
                reply_to,
                content,
                chunk_index=chunk_index,
                chunk_count=chunk_count,
            ),
            session_key,
            chat_id,
            reply_to,
            thread_id,
            content,
            "pending",
            "",
            chunk_index,
            chunk_count,
        )
        now = time.time()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                (obligation.obligation_id,),
            ).fetchone()
            if existing is not None and existing[0] in {
                "delivered",
                "attempting",
                "failed",
                "unknown",
            }:
                return replace(obligation, state=existing[0])
            connection.execute(
                "INSERT OR REPLACE INTO delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    obligation.obligation_id,
                    session_key,
                    chat_id,
                    reply_to,
                    thread_id,
                    content,
                    "pending",
                    chunk_index,
                    chunk_count,
                    "",
                    self._pid,
                    self._identity,
                    now,
                    now,
                ),
            )
        return obligation

    def _set_state(self, obligation_id: str, state: ObligationState, error: str = "") -> None:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                (obligation_id,),
            ).fetchone()
            if current is None or current[0] == "delivered":
                return
            if state == "pending" and current[0] != "pending":
                return
            connection.execute(
                "UPDATE delivery_obligations SET state=?, error=?, updated_at=? "
                "WHERE obligation_id=?",
                (state, error[:500], time.time(), obligation_id),
            )

    def mark_attempting(self, obligation_id: str) -> None:
        self._set_state(obligation_id, "attempting")

    def mark_delivered(self, obligation_id: str) -> None:
        self._set_state(obligation_id, "delivered")

    def mark_failed(self, obligation_id: str, error: str) -> None:
        self._set_state(obligation_id, "failed", error)

    def mark_unknown(self, obligation_id: str, error: str) -> None:
        self._set_state(obligation_id, "unknown", error)

    def sweep_recoverable(self) -> list[Obligation]:
        """Claim undelivered rows whose owner is gone, re-stamping them to this process."""
        self._prune()
        claimed: list[Obligation] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM delivery_obligations "
                "WHERE state IN ('pending','attempting','failed','unknown') ORDER BY created_at"
            ).fetchall()
            for row in rows:
                if self._owner_alive(int(row["owner_pid"]), str(row["owner_identity"])):
                    continue
                connection.execute(
                    "UPDATE delivery_obligations SET owner_pid=?, owner_identity=?, updated_at=? "
                    "WHERE obligation_id=?",
                    (self._pid, self._identity, time.time(), row["obligation_id"]),
                )
                claimed.append(
                    Obligation(
                        str(row["obligation_id"]),
                        str(row["session_key"]),
                        str(row["chat_id"]),
                        row["reply_to"],
                        row["thread_id"],
                        str(row["content"]),
                        row["state"],
                        str(row["error"]),
                        int(row["chunk_index"]),
                        int(row["chunk_count"]),
                    )
                )
        return claimed

    def _owner_alive(self, pid: int, identity: str) -> bool:
        if pid == self._pid:
            return True
        try:
            return process_identity(pid) == identity
        except (OSError, ValueError):
            return False

    def _prune(self) -> None:
        cutoff = time.time() - self.retention_seconds
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM delivery_obligations WHERE updated_at < ? "
                "AND state IN ('delivered','failed')",
                (cutoff,),
            )

    def rows(self, *, limit: int = 50) -> list[Obligation]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_obligations ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Obligation(
                str(r["obligation_id"]),
                str(r["session_key"]),
                str(r["chat_id"]),
                r["reply_to"],
                r["thread_id"],
                str(r["content"]),
                r["state"],
                str(r["error"]),
                int(r["chunk_index"]),
                int(r["chunk_count"]),
            )
            for r in rows
        ]


__all__ = ["RECOVERED_MARKER", "DeliveryLedger", "Obligation", "ObligationState"]
