"""Append-only session tree primitives for Run Agent."""

from __future__ import annotations

from run_agent_core.session.entries import (
    BaseSessionEntry,
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    LabelEntry,
    LeafEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    ThinkingLevelChangeEntry,
)
from run_agent_core.session.jsonl import SessionJsonlError, entry_from_json_line, entry_to_json_line
from run_agent_core.session.memory import SessionState
from run_agent_core.session.storage import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SessionStorage,
    load_session_entries,
)
from run_agent_core.session.tree import (
    SessionTree,
    SessionTreeError,
    entries_by_id,
    path_to_entry,
    resolve_active_leaf_id,
    stored_current_id,
)

__all__ = [
    "BaseSessionEntry",
    "BranchSummaryEntry",
    "CompactionEntry",
    "CustomEntry",
    "InMemorySessionStorage",
    "JsonlSessionStorage",
    "LabelEntry",
    "LeafEntry",
    "MessageEntry",
    "ModelChangeEntry",
    "SessionEntry",
    "SessionInfoEntry",
    "SessionJsonlError",
    "SessionState",
    "SessionStorage",
    "SessionTree",
    "SessionTreeError",
    "resolve_active_leaf_id",
    "stored_current_id",
    "ThinkingLevelChangeEntry",
    "entries_by_id",
    "entry_from_json_line",
    "entry_to_json_line",
    "load_session_entries",
    "path_to_entry",
]
