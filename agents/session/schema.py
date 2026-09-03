"""SQLite schema for sessions, lanes and append-only execution evidence."""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lanes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    parent_lane_id TEXT REFERENCES lanes(id) ON DELETE SET NULL,
    parent_entry_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lane_id TEXT NOT NULL REFERENCES lanes(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(lane_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_entries_lane_seq ON entries(lane_id, seq);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent_id);

CREATE TABLE IF NOT EXISTS operation_records (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lane_id TEXT NOT NULL REFERENCES lanes(id) ON DELETE CASCADE,
    run_id TEXT,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(lane_id, seq)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
