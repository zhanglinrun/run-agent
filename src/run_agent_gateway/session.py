"""Where a message came from, and which coding session answers it.

A ``SessionSource`` describes the chat a message arrived in. ``build_session_key`` turns
that into a stable key, and ``SessionStore`` maps keys to coding session IDs in JSONL so
the mapping survives a restart. The store also applies the reset policy: when a chat has
been quiet for long enough, or a daily boundary has passed, the next message starts a
fresh session.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from run_agent_gateway.config import SessionResetPolicy

ChatType = Literal["dm", "group"]


@dataclass(frozen=True, slots=True)
class SessionSource:
    platform: str
    chat_id: str
    chat_type: ChatType = "dm"
    chat_name: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    thread_id: str | None = None
    message_id: str | None = None

    @property
    def description(self) -> str:
        if self.chat_type == "dm":
            head = f"DM with {self.user_name or self.user_id or 'user'}"
        else:
            head = f"group {self.chat_name or self.chat_id}"
        return f"{head}, thread {self.thread_id}" if self.thread_id else head

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionSource:
        chat_type: ChatType = "group" if data.get("chat_type") == "group" else "dm"
        return cls(
            platform=str(data.get("platform", "")),
            chat_id=str(data.get("chat_id", "")),
            chat_type=chat_type,
            chat_name=_optional(data.get("chat_name")),
            user_id=_optional(data.get("user_id")),
            user_name=_optional(data.get("user_name")),
            thread_id=_optional(data.get("thread_id")),
            message_id=_optional(data.get("message_id")),
        )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def build_session_key(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> str:
    """Derive the conversation key for a message source.

    Direct messages get one session per chat (and per thread inside it). Group chats
    get one session per chat, split per participant when ``group_sessions_per_user`` is
    on. Threads inside a group are shared by everyone in the thread unless
    ``thread_sessions_per_user`` asks for per-user isolation there as well.
    """
    parts = [source.platform, source.chat_type, source.chat_id]
    if source.thread_id:
        parts.append(source.thread_id)
    if source.chat_type != "dm":
        isolate = group_sessions_per_user and (not source.thread_id or thread_sessions_per_user)
        if isolate and source.user_id:
            parts.append(source.user_id)
    return ":".join(parts)


@dataclass(slots=True)
class SessionEntry:
    session_key: str
    session_id: str
    created_at: float
    updated_at: float
    origin: SessionSource
    was_auto_reset: bool = field(default=False, compare=False)
    auto_reset_reason: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin": self.origin.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionEntry:
        origin = data.get("origin")
        return cls(
            session_key=str(data["session_key"]),
            session_id=str(data["session_id"]),
            created_at=float(str(data.get("created_at", 0.0))),
            updated_at=float(str(data.get("updated_at", 0.0))),
            origin=SessionSource.from_dict(origin if isinstance(origin, dict) else {}),
        )


def _store_paths(path: Path) -> tuple[Path, Path]:
    """Resolve the JSONL file and the legacy JSON import path."""
    if path.suffix.lower() == ".jsonl":
        return path, path.with_name("sessions.json")
    if path.suffix.lower() in {".json", ".sqlite3"}:
        return path.with_suffix(".jsonl"), path.with_name("sessions.json")
    return path.with_name(f"{path.name}.jsonl") if path.suffix else path.with_suffix(".jsonl"), (
        path.with_name("sessions.json")
    )


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as file:
        file.write(line)
        file.flush()


def _read_jsonl_entries(path: Path) -> dict[str, SessionEntry]:
    entries: dict[str, SessionEntry] = {}
    if not path.is_file():
        return entries
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return entries
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
        try:
            entry = SessionEntry.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            continue
        entries[entry.session_key] = entry
    return entries


class SessionStore:
    """Chat-key to coding-session mapping persisted as append-only JSONL."""

    def __init__(
        self,
        path: Path,
        policy: SessionResetPolicy,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path, self._legacy_json = _store_paths(path)
        self.policy = policy
        self._now = now or datetime.now
        self._entries: dict[str, SessionEntry] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        self._entries = _read_jsonl_entries(self.path)
        imported = self._import_legacy_json()
        if imported:
            self._upsert_many(imported)

    def _import_legacy_json(self) -> list[SessionEntry]:
        if not self._legacy_json.is_file():
            return []
        try:
            document = json.loads(self._legacy_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        sessions = document.get("sessions", document) if isinstance(document, dict) else {}
        if not isinstance(sessions, dict):
            return []
        imported: list[SessionEntry] = []
        for key, raw in sessions.items():
            session_key = str(key)
            if session_key.startswith("_") or session_key in self._entries:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                entry = SessionEntry.from_dict({**raw, "session_key": session_key})
            except (KeyError, TypeError, ValueError):
                continue
            self._entries[session_key] = entry
            imported.append(entry)
        return imported

    def _upsert(self, entry: SessionEntry) -> None:
        self._upsert_many((entry,))

    def _upsert_many(self, entries: tuple[SessionEntry, ...] | list[SessionEntry]) -> None:
        if not entries:
            return
        for entry in entries:
            _append_jsonl(self.path, entry.to_dict())

    def get(self, session_key: str) -> SessionEntry | None:
        return self._entries.get(session_key)

    def entries(self) -> tuple[SessionEntry, ...]:
        return tuple(self._entries.values())

    def get_or_create(self, session_key: str, source: SessionSource) -> SessionEntry:
        """Return the live entry for a chat, starting a new session when policy says so."""
        entry = self._entries.get(session_key)
        if entry is None:
            entry = self._create(session_key, source)
        else:
            reason = self._should_reset(entry)
            if reason is not None:
                entry = self._create(session_key, source)
                entry.was_auto_reset = True
                entry.auto_reset_reason = reason
        return entry

    def reset(self, session_key: str, source: SessionSource) -> SessionEntry:
        """Start a fresh session for a chat on the user's request."""
        return self._create(session_key, source)

    def replace(self, session_key: str, session_id: str) -> SessionEntry:
        """Atomically persist a routing change after a session switch succeeds."""
        entry = self._entries.get(session_key)
        if entry is None:
            raise KeyError(f"unknown session key: {session_key}")
        previous_id = entry.session_id
        previous_updated_at = entry.updated_at
        entry.session_id = session_id
        entry.updated_at = time.time()
        try:
            self._upsert(entry)
        except BaseException:
            entry.session_id = previous_id
            entry.updated_at = previous_updated_at
            raise
        return entry

    def touch(self, session_key: str) -> None:
        entry = self._entries.get(session_key)
        if entry is not None:
            entry.updated_at = time.time()
            self._upsert(entry)

    def _create(self, session_key: str, source: SessionSource) -> SessionEntry:
        moment = time.time()
        entry = SessionEntry(session_key, uuid4().hex, moment, moment, source)
        previous = self._entries.get(session_key)
        self._entries[session_key] = entry
        try:
            self._upsert(entry)
        except BaseException:
            if previous is None:
                self._entries.pop(session_key, None)
            else:
                self._entries[session_key] = previous
            raise
        return entry

    def _should_reset(self, entry: SessionEntry) -> str | None:
        policy = self.policy
        if policy.mode == "none":
            return None
        now = self._now()
        updated = datetime.fromtimestamp(entry.updated_at, tz=now.tzinfo)
        idle_deadline = updated + timedelta(minutes=policy.idle_minutes)
        if policy.mode in {"idle", "both"} and now > idle_deadline:
            return "idle"
        if policy.mode in {"daily", "both"}:
            boundary = now.replace(hour=policy.at_hour, minute=0, second=0, microsecond=0)
            if now < boundary:
                boundary -= timedelta(days=1)
            if updated < boundary:
                return "daily"
        return None


__all__ = ["ChatType", "SessionEntry", "SessionSource", "SessionStore", "build_session_key"]
