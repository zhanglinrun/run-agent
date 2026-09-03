"""Serializable event-tree records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EntryType(str, Enum):
    MESSAGE = "message"
    COMPACTION = "compaction"
    BRANCH_SUMMARY = "branch_summary"
    CUSTOM = "custom"


class OperationType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    PERMISSION_DECIDED = "permission_decided"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_FINISHED = "verification_finished"
    CORRECTION_STARTED = "correction_started"
    CORRECTION_FINISHED = "correction_finished"


@dataclass(frozen=True)
class Entry:
    id: str
    session_id: str
    lane_id: str
    parent_id: str | None
    seq: int
    type: EntryType
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class OperationRecord:
    id: str
    session_id: str
    lane_id: str
    run_id: str | None
    seq: int
    type: OperationType
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
