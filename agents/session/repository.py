"""Transactional SQLite repository with structured diagnostics."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
import re
from typing import Any

from ..runtime.contracts import new_id, utc_now
from .entries import Entry, EntryType, OperationRecord, OperationType
from .schema import SCHEMA_SQL, SCHEMA_VERSION


class SessionRepository:
    """Own one SQLite database and append all session events transactionally."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(SCHEMA_SQL)
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SessionRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_session(self, *, session_id: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        session_id = session_id or new_id("session")
        now = utc_now()
        main_lane_id = f"{session_id}:main"
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO sessions(id, created_at, updated_at, metadata_json) VALUES (?, ?, ?, ?)",
                (session_id, now, now, self._dumps(metadata or {})),
            )
            self._connection.execute(
                "INSERT INTO lanes(id, session_id, name, created_at) VALUES (?, ?, ?, ?)",
                (main_lane_id, session_id, "main", now),
            )
        return session_id

    def ensure_session(self, session_id: str, *, metadata: dict[str, Any] | None = None) -> str:
        if self.get_session(session_id) is None:
            return self.create_session(session_id=session_id, metadata=metadata)
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json"))
        except Exception:
            result["metadata"] = {}
        return result

    def list_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT id, created_at, updated_at, metadata_json FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
        sessions: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._json(row["metadata_json"])
            entries = self.list_entries(str(row["id"]), lane_id="main")
            messages = [
                item.payload.get("message")
                for item in entries
                if item.type == EntryType.MESSAGE and isinstance(item.payload.get("message"), dict)
            ]
            preview = "(no user message)"
            for message in reversed(messages):
                if message.get("role") == "user" and str(message.get("content") or "").strip():
                    text = " ".join(str(message["content"]).split())
                    preview = text if len(text) <= 60 else text[:57] + "..."
                    break
            updated = str(row["updated_at"])
            try:
                updated_display = datetime.fromisoformat(updated.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                updated_display = updated
            sessions.append({
                "id": str(row["id"]),
                "model": str(metadata.get("model") or ""),
                "message_count": len(messages),
                "preview": preview,
                "startTime": str(row["created_at"]),
                "updated_at": updated,
                "updated_at_str": updated_display,
            })
        return sessions

    def create_lane(self, session_id: str, *, name: str, parent_lane_id: str | None = "main", parent_entry_id: str | None = None) -> str:
        lane_id = new_id("lane")
        now = utc_now()
        parent_lane_id = self._lane_key(session_id, parent_lane_id) if parent_lane_id else None
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO lanes(id, session_id, parent_lane_id, parent_entry_id, name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lane_id, session_id, parent_lane_id, parent_entry_id, name, now),
            )
        return lane_id

    def append_entry(self, session_id: str, lane_id: str, entry_type: EntryType | str, payload: dict[str, Any], *, parent_id: str | None = None) -> Entry:
        now = utc_now()
        lane_id = self._lane_key(session_id, lane_id)
        with self._lock, self._connection:
            row = self._connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM entries WHERE lane_id = ?", (lane_id,)).fetchone()
            seq = int(row["next_seq"])
            entry = Entry(new_id("entry"), session_id, lane_id, parent_id, seq, EntryType(entry_type), dict(payload), now)
            self._connection.execute(
                "INSERT INTO entries(id, session_id, lane_id, parent_id, seq, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, session_id, lane_id, parent_id, seq, entry.type.value, self._dumps(entry.payload), now),
            )
            self._touch_session(session_id, now)
        return entry

    def append_operation(self, session_id: str, lane_id: str, operation_type: OperationType | str, payload: dict[str, Any], *, run_id: str | None = None) -> OperationRecord:
        now = utc_now()
        lane_id = self._lane_key(session_id, lane_id)
        with self._lock, self._connection:
            row = self._connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM operation_records WHERE lane_id = ?", (lane_id,)).fetchone()
            seq = int(row["next_seq"])
            record = OperationRecord(new_id("op"), session_id, lane_id, run_id, seq, OperationType(operation_type), dict(payload), now)
            self._connection.execute(
                "INSERT INTO operation_records(id, session_id, lane_id, run_id, seq, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, session_id, lane_id, run_id, seq, record.type.value, self._dumps(record.payload), now),
            )
            self._touch_session(session_id, now)
        return record

    def list_entries(self, session_id: str, *, lane_id: str = "main", limit: int | None = None) -> list[Entry]:
        lane_id = self._lane_key(session_id, lane_id)
        order = "DESC" if limit is not None else "ASC"
        sql = f"SELECT * FROM entries WHERE session_id = ? AND lane_id = ? ORDER BY seq {order}"
        params: list[Any] = [session_id, lane_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._connection.execute(sql, params).fetchall()
        if limit is not None:
            rows = list(reversed(rows))
        return [self._entry_from_row(row) for row in rows]

    def latest_entry(self, session_id: str, *, lane_id: str = "main") -> Entry | None:
        rows = self.list_entries(session_id, lane_id=lane_id, limit=1)
        return rows[-1] if rows else None

    def list_branch(self, session_id: str, *, lane_id: str = "main") -> list[Entry]:
        lane_id = self._lane_key(session_id, lane_id)
        lane = self._connection.execute("SELECT parent_lane_id, parent_entry_id FROM lanes WHERE id = ? AND session_id = ?", (lane_id, session_id)).fetchone()
        if lane is None:
            return []
        entries = self.list_entries(session_id, lane_id=lane_id)
        if lane["parent_lane_id"] and lane["parent_lane_id"] != lane_id:
            parent = self.list_branch(session_id, lane_id=lane["parent_lane_id"])
            stop = lane["parent_entry_id"]
            if stop:
                parent = parent[: next((index + 1 for index, item in enumerate(parent) if item.id == stop), len(parent))]
            return parent + entries
        return entries

    def add_artifact(self, session_id: str, *, kind: str, path: str, sha256: str | None = None, run_id: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        artifact_id = new_id("artifact")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO artifacts(id, session_id, run_id, kind, path, sha256, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, session_id, run_id, kind, path, sha256, self._dumps(metadata or {}), utc_now()),
            )
        return artifact_id

    def _touch_session(self, session_id: str, timestamp: str) -> None:
        self._connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))

    @staticmethod
    def _lane_key(session_id: str, lane_id: str) -> str:
        return f"{session_id}:main" if lane_id == "main" else lane_id

    @staticmethod
    def _json(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _entry_from_row(self, row: sqlite3.Row) -> Entry:
        try:
            entry_type = EntryType(row["type"])
            payload = self._json(row["payload_json"])
        except Exception as exc:
            entry_type = EntryType.CUSTOM
            payload = {"diagnostic": {"kind": "entry_decode_error", "message": str(exc), "entry_id": row["id"]}}
        return Entry(row["id"], row["session_id"], row["lane_id"], row["parent_id"], int(row["seq"]), entry_type, payload, row["created_at"])

    @classmethod
    def _safe_value(cls, value: Any, *, key: str = "") -> Any:
        if re.search(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie|credential)", key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): cls._safe_value(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe_value(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
            return value[:20000] if len(value) > 20000 else value
        return value

    @classmethod
    def _dumps(cls, value: Any) -> str:
        return json.dumps(cls._safe_value(value), ensure_ascii=False, sort_keys=True, default=str)
