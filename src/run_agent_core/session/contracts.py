"""Transactional session contracts, independent of the persistence engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

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


class SessionRepository(Protocol):
    async def get_head(self, session_id: str, branch_id: str = "main") -> BranchHead: ...

    async def append_entries(
        self,
        entries: Sequence[SessionEntry],
        *,
        token: RunToken,
        branch_id: str = "main",
        expected_head: str | None,
    ) -> AppendReceipt: ...

    async def read_entries(
        self,
        session_id: str,
        *,
        branch_id: str | None = None,
        after_seq: int = 0,
        through_seq: int | None = None,
        limit: int = 1000,
    ) -> EntryPage: ...
