"""Bound transactional session handles and a deterministic test implementation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Protocol
from uuid import uuid4

from run_agent_core.session.contracts import (
    AppendReceipt,
    BranchHead,
    CompletionReceipt,
    EntryPage,
    RunOutcome,
    RunToken,
    SessionConflict,
    StaleRunToken,
)
from run_agent_core.session.entries import SessionEntry
from run_agent_core.session.tree import path_to_entry
from run_agent_core.types import JSONValue


class SessionStorage(Protocol):
    """A session writer with explicit branch, version and completion semantics."""

    @property
    def session_id(self) -> str: ...

    @property
    def branch_id(self) -> str: ...

    @property
    def token(self) -> RunToken: ...

    async def read_entries(self, *, after_seq: int = 0, limit: int = 1000) -> EntryPage: ...

    async def get_head(self) -> BranchHead: ...

    async def append_entries(
        self, entries: Sequence[SessionEntry], *, expected_head: str | None, token: RunToken
    ) -> AppendReceipt: ...

    async def fork(
        self, at_entry_id: str | None, *, token: RunToken, entries: Sequence[SessionEntry] = ()
    ) -> BranchHead: ...

    async def begin_run(self, run_id: str) -> RunToken: ...

    async def complete_run(self, outcome: RunOutcome) -> CompletionReceipt: ...

    async def record_context(
        self,
        payload: dict[str, JSONValue],
        *,
        token: RunToken,
        expected_head: str | None,
        builder_version: str,
    ) -> str: ...

    async def aclose(self) -> None: ...


async def load_session_entries(storage: SessionStorage) -> list[SessionEntry]:
    """Materialize history explicitly for cold open and tree inspection."""
    entries: list[SessionEntry] = []
    after = 0
    while True:
        page = await storage.read_entries(after_seq=after)
        entries.extend(page.entries)
        if page.next_seq is None:
            return entries
        after = page.next_seq


class InMemorySessionStorage:
    """Tests use the same expected-head and run-fence contract as durable hosts."""

    def __init__(
        self, entries: Sequence[SessionEntry] = (), *, session_id: str | None = None
    ) -> None:
        self.session_id = session_id or uuid4().hex
        self.branch_id = "main"
        self.token = RunToken(self.session_id, "memory", "initial", 1)
        self.entries = [
            entry.model_copy(deep=True, update={"seq": i + 1}) for i, entry in enumerate(entries)
        ]
        self._heads: dict[str, str | None] = {"main": self.entries[-1].id if self.entries else None}
        self._closed = False
        self._lock = asyncio.Lock()
        self.outcomes: dict[str, CompletionReceipt] = {}
        self._outcome_inputs: dict[str, RunOutcome] = {}
        self.snapshots: dict[str, tuple[RunToken, str, dict[str, JSONValue]]] = {}

    def _check(self, token: RunToken) -> None:
        if self._closed or token != self.token:
            raise StaleRunToken("Session write qualification has expired")

    async def get_head(self) -> BranchHead:
        return BranchHead(self.session_id, self.branch_id, self._heads[self.branch_id])

    async def read_entries(self, *, after_seq: int = 0, limit: int = 1000) -> EntryPage:
        if after_seq < 0 or not 1 <= limit <= 10_000:
            raise ValueError("Invalid history page bounds")
        rows = [
            entry.model_copy(deep=True) for entry in self.entries if (entry.seq or 0) > after_seq
        ]
        page = rows[:limit]
        return EntryPage(tuple(page), page[-1].seq if len(rows) > limit else None)

    def _append(
        self, entries: Sequence[SessionEntry], expected_head: str | None, token: RunToken
    ) -> AppendReceipt:
        self._check(token)
        existing = {entry.id: entry for entry in self.entries}
        parent = expected_head
        for entry in entries:
            if entry.parent_id != parent:
                raise SessionConflict("Batch does not extend its expected parent chain")
            parent = entry.id
        if len({entry.id for entry in entries}) != len(entries):
            raise SessionConflict("Duplicate event identity in one batch")
        present = [existing.get(entry.id) for entry in entries]
        for entry, old in zip(entries, present, strict=True):
            if old is not None and old.model_dump(exclude={"seq"}) != entry.model_dump(
                exclude={"seq"}
            ):
                raise SessionConflict("An event identity has different content")
        if entries and all(item is not None for item in present):
            return AppendReceipt(
                self.session_id,
                self.branch_id,
                parent,
                tuple(e.id for e in entries),
                tuple(e.seq or 0 for e in present if e is not None),
                False,
            )
        if (
            any(item is not None for item in present)
            or self._heads[self.branch_id] != expected_head
        ):
            raise SessionConflict("Branch head changed")
        sequences = tuple(range(len(self.entries) + 1, len(self.entries) + len(entries) + 1))
        self.entries.extend(
            entry.model_copy(deep=True, update={"seq": seq})
            for entry, seq in zip(entries, sequences, strict=True)
        )
        self._heads[self.branch_id] = parent
        return AppendReceipt(
            self.session_id,
            self.branch_id,
            parent,
            tuple(e.id for e in entries),
            sequences,
            bool(entries),
        )

    async def append_entries(
        self, entries: Sequence[SessionEntry], *, expected_head: str | None, token: RunToken
    ) -> AppendReceipt:
        async with self._lock:
            return self._append(entries, expected_head, token)

    async def fork(
        self, at_entry_id: str | None, *, token: RunToken, entries: Sequence[SessionEntry] = ()
    ) -> BranchHead:
        async with self._lock:
            self._check(token)
            if at_entry_id is not None:
                path_to_entry(self.entries, at_entry_id)
            previous_branch = self.branch_id
            self.branch_id = uuid4().hex
            self._heads[self.branch_id] = at_entry_id
            try:
                self._append(entries, at_entry_id, token)
            except BaseException:
                del self._heads[self.branch_id]
                self.branch_id = previous_branch
                raise
            return await self.get_head()

    async def begin_run(self, run_id: str) -> RunToken:
        async with self._lock:
            self._check(self.token)
            self.token = RunToken(self.session_id, "memory", run_id, self.token.generation + 1)
            return self.token

    async def complete_run(self, outcome: RunOutcome) -> CompletionReceipt:
        async with self._lock:
            previous = self._outcome_inputs.get(outcome.token.run_id)
            if previous is not None:
                if previous != outcome:
                    raise SessionConflict("Run already has a different outcome")
                return self.outcomes[outcome.token.run_id]
            self._check(outcome.token)
            if outcome.branch_id != self.branch_id:
                raise SessionConflict("Run branch changed")
            if outcome.snapshot_id is not None:
                snapshot = self.snapshots.get(outcome.snapshot_id)
                if snapshot is None or snapshot[:2] != (outcome.token, outcome.branch_id):
                    raise SessionConflict("Completion snapshot does not belong to this run")
            self._append(outcome.entries, outcome.expected_head, outcome.token)
            receipt = CompletionReceipt(
                outcome.token.run_id,
                self.session_id,
                self.branch_id,
                outcome.status,
                self._heads[self.branch_id],
                len(self.entries),
                outcome.snapshot_id,
            )
            self.outcomes[outcome.token.run_id] = receipt
            self._outcome_inputs[outcome.token.run_id] = outcome
            self.token = RunToken(
                self.session_id,
                self.token.owner_id,
                f"idle-{self.token.run_id}",
                self.token.generation + 1,
            )
            return receipt

    async def record_context(
        self,
        payload: dict[str, JSONValue],
        *,
        token: RunToken,
        expected_head: str | None,
        builder_version: str,
    ) -> str:
        async with self._lock:
            self._check(token)
            if self._heads[self.branch_id] != expected_head:
                raise SessionConflict("Snapshot history changed before commit")
            snapshot_id = uuid4().hex
            self.snapshots[snapshot_id] = token, self.branch_id, json.loads(json.dumps(payload))
            return snapshot_id

    async def aclose(self) -> None:
        self._closed = True
