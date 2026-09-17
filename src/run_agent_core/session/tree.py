"""Session tree traversal helpers."""

from __future__ import annotations

from collections.abc import Sequence

from run_agent_core.session.entries import (
    CompactionEntry,
    LeafEntry,
    SessionEntry,
    SessionInfoEntry,
)


class SessionTreeError(ValueError):
    """Raised when session entries do not form a valid traversable tree."""


def entries_by_id(entries: list[SessionEntry]) -> dict[str, SessionEntry]:
    """Return entries keyed by id, rejecting duplicates."""
    result: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.id in result:
            raise SessionTreeError(f"Duplicate session entry id: {entry.id}")
        result[entry.id] = entry
    return result


def path_to_entry(entries: list[SessionEntry], leaf_id: str) -> list[SessionEntry]:
    """Return the root-to-leaf path for `leaf_id`."""
    by_id = entries_by_id(entries)
    path: list[SessionEntry] = []
    seen: set[str] = set()
    current_id: str | None = leaf_id

    while current_id is not None:
        if current_id in seen:
            raise SessionTreeError(f"Cycle detected at session entry: {current_id}")
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            raise SessionTreeError(f"Missing session entry: {current_id}")
        path.append(entry)
        current_id = entry.parent_id

    path.reverse()
    return path


def resolve_active_leaf_id(entries: Sequence[SessionEntry]) -> str | None:
    """Return the durable active leaf: latest LeafEntry, else legacy SessionInfo.current_id."""
    known = {entry.id for entry in entries}
    for entry in reversed(entries):
        if not isinstance(entry, LeafEntry):
            continue
        if entry.entry_id is None or entry.entry_id in known:
            return entry.entry_id
    for entry in entries:
        if isinstance(entry, SessionInfoEntry) and entry.current_id in known:
            return entry.current_id
    for entry in reversed(entries):
        if not isinstance(entry, LeafEntry):
            return entry.id
    return None


def stored_current_id(entries: Sequence[SessionEntry]) -> str | None:
    """Return the durable active leaf id for resume and OCC."""
    return resolve_active_leaf_id(entries)


class SessionTree:
    """In-memory session DAG: entries keyed by id plus a current pointer."""

    def __init__(
        self,
        entries: Sequence[SessionEntry] = (),
        *,
        current_id: str | None = None,
    ) -> None:
        self.entries: dict[str, SessionEntry] = {}
        self.current_id: str | None = None
        self.root_id: str | None = None
        for entry in entries:
            self.add(entry, move_current=False)
        if current_id is not None:
            if entries and current_id not in self.entries:
                raise SessionTreeError(f"Unknown current entry: {current_id}")
            self.current_id = current_id
        elif entries:
            self.current_id = entries[-1].id

    def add(self, entry: SessionEntry, *, move_current: bool = True) -> SessionEntry:
        """Insert an entry. New messages become current unless `move_current` is false."""
        self.entries[entry.id] = entry
        if self.root_id is None:
            self.root_id = entry.id
        if move_current:
            self.current_id = entry.id
        return entry

    def rewind(self, entry_id: str) -> None:
        """Move current_id; abandoned branches stay in `entries`."""
        if entry_id not in self.entries:
            raise SessionTreeError(f"Unknown session entry: {entry_id}")
        self.current_id = entry_id

    def path(self, entry_id: str | None = None) -> list[SessionEntry]:
        """Root → current (or `entry_id`) path."""
        target = self.current_id if entry_id is None else entry_id
        if target is None:
            return []
        return path_to_entry(list(self.entries.values()), target)

    def compaction_floor(self) -> str | None:
        """Latest compaction entry id, if any."""
        latest: CompactionEntry | None = None
        for entry in self.entries.values():
            if isinstance(entry, CompactionEntry) and (
                latest is None or entry.timestamp >= latest.timestamp
            ):
                latest = entry
        return None if latest is None else latest.id

    def after_compaction_floor(self, entry_id: str) -> bool:
        """True when `entry_id` is the floor or grew from it."""
        floor = self.compaction_floor()
        if floor is None or entry_id == floor:
            return True
        current = self.entries.get(entry_id)
        while current is not None and current.parent_id is not None:
            if current.parent_id == floor:
                return True
            current = self.entries.get(current.parent_id)
        return False
