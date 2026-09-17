"""Durable delivery obligations for final replies.

A reply that the model already produced but the platform has not confirmed is the one
thing the gateway can lose without a trace. The ledger appends three checkpoints around a
send into JSONL: ``pending`` before the first attempt, ``attempting`` right before the
await, then ``delivered`` or ``failed``. On startup the rows whose owning process is dead
are handed back for redelivery. A ``pending`` row is resent plainly; ``attempting`` and
``failed`` rows carry a visible recovered-reply marker, because the platform may already
have the message and a silent duplicate would be worse than an honest one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from run_agent_coding.host.process_identity import process_identity

ObligationState = Literal["pending", "attempting", "delivered", "failed", "unknown"]
RECOVERED_MARKER = "（以下是网关重启前未确认送达的回复，可能与之前的消息重复。）\n\n"


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


@dataclass(slots=True)
class _Row:
    obligation: Obligation
    owner_pid: int
    owner_identity: str
    created_at: float
    updated_at: float


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as file:
        file.write(line)
        file.flush()


class DeliveryLedger:
    def __init__(self, path: Path, *, retention_seconds: float = 7 * 24 * 3600) -> None:
        self.path = path
        self.retention_seconds = retention_seconds
        self._pid = os.getpid()
        self._identity = process_identity(self._pid) or f"pid:{self._pid}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: dict[str, _Row] = {}
        self._load()

    def _load(self) -> None:
        self._rows = {}
        if not self.path.is_file():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            row = self._row_from_payload(payload)
            if row is not None:
                self._rows[row.obligation.obligation_id] = row

    def _row_from_payload(self, payload: dict[str, Any]) -> _Row | None:
        try:
            state = str(payload["state"])
            if state not in {"pending", "attempting", "delivered", "failed", "unknown"}:
                return None
            obligation = Obligation(
                str(payload["obligation_id"]),
                str(payload["session_key"]),
                str(payload["chat_id"]),
                payload.get("reply_to"),
                payload.get("thread_id"),
                str(payload["content"]),
                state,  # type: ignore[arg-type]
                str(payload.get("error") or ""),
                int(payload.get("chunk_index") or 0),
                int(payload.get("chunk_count") or 1),
            )
            return _Row(
                obligation,
                int(payload["owner_pid"]),
                str(payload["owner_identity"]),
                float(payload["created_at"]),
                float(payload["updated_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _payload(self, row: _Row) -> dict[str, Any]:
        obligation = row.obligation
        return {
            "obligation_id": obligation.obligation_id,
            "session_key": obligation.session_key,
            "chat_id": obligation.chat_id,
            "reply_to": obligation.reply_to,
            "thread_id": obligation.thread_id,
            "content": obligation.content,
            "state": obligation.state,
            "error": obligation.error,
            "chunk_index": obligation.chunk_index,
            "chunk_count": obligation.chunk_count,
            "owner_pid": row.owner_pid,
            "owner_identity": row.owner_identity,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _write(self, row: _Row) -> None:
        self._rows[row.obligation.obligation_id] = row
        _append_jsonl(self.path, self._payload(row))

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
        existing = self._rows.get(obligation.obligation_id)
        if existing is not None and existing.obligation.state in {
            "delivered",
            "attempting",
            "failed",
            "unknown",
        }:
            return existing.obligation
        now = time.time()
        self._write(
            _Row(obligation, self._pid, self._identity, now, now),
        )
        return obligation

    def _set_state(self, obligation_id: str, state: ObligationState, error: str = "") -> None:
        current = self._rows.get(obligation_id)
        if current is None or current.obligation.state == "delivered":
            return
        if state == "pending" and current.obligation.state != "pending":
            return
        obligation = replace(current.obligation, state=state, error=error[:500])
        self._write(
            _Row(
                obligation,
                current.owner_pid,
                current.owner_identity,
                current.created_at,
                time.time(),
            )
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
        now = time.time()
        for row in sorted(self._rows.values(), key=lambda item: item.created_at):
            if row.obligation.state not in {"pending", "attempting", "failed", "unknown"}:
                continue
            if self._owner_alive(row.owner_pid, row.owner_identity):
                continue
            updated = _Row(row.obligation, self._pid, self._identity, row.created_at, now)
            self._write(updated)
            claimed.append(row.obligation)
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
        stale = [
            obligation_id
            for obligation_id, row in self._rows.items()
            if row.updated_at < cutoff and row.obligation.state in {"delivered", "failed"}
        ]
        for obligation_id in stale:
            del self._rows[obligation_id]

    def rows(self, *, limit: int = 50) -> list[Obligation]:
        ordered = sorted(self._rows.values(), key=lambda item: item.created_at, reverse=True)
        return [row.obligation for row in ordered[:limit]]


__all__ = ["RECOVERED_MARKER", "DeliveryLedger", "Obligation", "ObligationState"]
