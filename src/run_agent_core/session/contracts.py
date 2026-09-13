"""Transactional session contracts, independent of the persistence engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from run_agent_core.session.entries import SessionEntry


class SessionConflict(RuntimeError):
    """An expected head, event identity or write owner no longer matches."""


class StaleRunToken(SessionConflict):
    """The caller no longer owns this session's execution qualification."""


@dataclass(frozen=True, slots=True)
class RunToken:
    session_id: str
    owner_id: str
    run_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class BranchHead:
    session_id: str
    branch_id: str
    entry_id: str | None


@dataclass(frozen=True, slots=True)
class AppendReceipt:
    session_id: str
    branch_id: str
    head_id: str | None
    entry_ids: tuple[str, ...]
    sequences: tuple[int, ...]
    applied: bool


@dataclass(frozen=True, slots=True)
class EntryPage:
    entries: tuple[SessionEntry, ...]
    next_seq: int | None


RunStatus = Literal["succeeded", "failed", "cancelled", "interrupted", "outcome_unknown"]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    token: RunToken
    branch_id: str
    status: RunStatus
    expected_head: str | None
    entries: tuple[SessionEntry, ...] = ()
    error: str | None = None
    snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    run_id: str
    session_id: str
    branch_id: str
    status: RunStatus
    head_id: str | None
    watermark: int
    snapshot_id: str | None = None
