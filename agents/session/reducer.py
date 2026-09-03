"""Project SQLite entries into provider-neutral model messages."""

from __future__ import annotations

from typing import Any

from .entries import EntryType
from .repository import SessionRepository


class SessionReducer:
    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def messages(self, session_id: str, *, lane_id: str = "main") -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for entry in self.repository.list_branch(session_id, lane_id=lane_id):
            if entry.type == EntryType.MESSAGE:
                value = entry.payload.get("message")
                if isinstance(value, dict):
                    messages.append(dict(value))
            elif entry.type in {EntryType.COMPACTION, EntryType.BRANCH_SUMMARY}:
                summary = entry.payload.get("summary") or entry.payload.get("content")
                if summary:
                    # A fold replaces the projected raw prefix while the append-only
                    # entries remain available for audit and branch navigation.
                    messages = [
                        {
                            "role": "system",
                            "content": f"<session-summary>\n{summary}\n</session-summary>",
                        }
                    ]
        return messages

    def append_message(self, session_id: str, message: dict[str, Any], *, lane_id: str = "main"):
        latest = self.repository.latest_entry(session_id, lane_id=lane_id)
        parent_id = latest.id if latest else None
        return self.repository.append_entry(session_id, lane_id, EntryType.MESSAGE, {"message": dict(message)}, parent_id=parent_id)

    def append_compaction(self, session_id: str, summary: str, *, lane_id: str = "main", details: dict[str, Any] | None = None):
        latest = self.repository.latest_entry(session_id, lane_id=lane_id)
        parent_id = latest.id if latest else None
        return self.repository.append_entry(session_id, lane_id, EntryType.COMPACTION, {"summary": summary, **(details or {})}, parent_id=parent_id)
