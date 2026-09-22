"""Session writer over append-only JSONL or in-memory storage."""

from __future__ import annotations

from collections.abc import Sequence
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
from run_agent_core.session.entries import LeafEntry, RunCommitEntry, SessionEntry
from run_agent_core.session.storage import SessionStorage
from run_agent_core.session.tree import resolve_active_leaf_id
from run_agent_core.types import JSONValue


class SessionWriter:
    """Coding-facing handle: Pi append storage plus a lightweight run id."""

    def __init__(self, inner: SessionStorage, session_id: str, *, owner_id: str = "local") -> None:
        self._inner = inner
        self.session_id = session_id
        self.branch_id = "main"
        self.token = RunToken(session_id, owner_id, "idle", 1)
        self.closed = False
        self._owner_id = owner_id
        self.snapshots: dict[str, dict[str, JSONValue]] = {}
        self._completions: dict[int, CompletionReceipt] = {}
        self._completion_keys: dict[int, tuple[object, ...]] = {}
        self._run_start_heads: dict[int, str | None] = {}

    async def append(self, entry: SessionEntry) -> None:
        head = await self.get_head()
        await self.append_entries((entry,), expected_head=head.entry_id, token=self.token)

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        head = await self.get_head()
        await self.append_entries(entries, expected_head=head.entry_id, token=self.token)

    async def compare_and_append(
        self, entries: Sequence[SessionEntry], expected_head: str | None
    ) -> list[SessionEntry]:
        self._check_token(self.token)
        return await self._inner.compare_and_append(entries, expected_head)

    async def read_all(self) -> list[SessionEntry]:
        entries = await self._inner.read_all()
        return [
            entry.model_copy(deep=True, update={"seq": index})
            for index, entry in enumerate(entries, start=1)
        ]

    async def read_entries(self, *, after_seq: int = 0, limit: int = 1000) -> EntryPage:
        rows = [entry for entry in await self.read_all() if (entry.seq or 0) > after_seq]
        page = rows[:limit]
        if not page:
            return EntryPage((), None)
        return EntryPage(tuple(page), page[-1].seq if len(rows) > limit else None)

    async def get_head(self) -> BranchHead:
        entries = await self.read_all()
        return BranchHead(self.session_id, self.branch_id, resolve_active_leaf_id(entries))

    def _check_token(self, token: RunToken) -> None:
        if self.closed:
            raise StaleRunToken("Session writer is closed")
        if token.session_id != self.session_id:
            raise StaleRunToken("Token session mismatch")
        if token.generation in self._completions:
            raise StaleRunToken("Run token has been retired")
        if token != self.token:
            raise StaleRunToken("This callback belongs to an expired run")

    async def append_entries(
        self, entries: Sequence[SessionEntry], *, expected_head: str | None, token: RunToken
    ) -> AppendReceipt:
        self._check_token(token)
        batch = tuple(entries)
        numbered = await self._inner.compare_and_append(batch, expected_head)
        by_id = {entry.id: entry for entry in numbered}
        sequences = tuple(by_id[entry.id].seq or 0 for entry in batch)
        return AppendReceipt(
            self.session_id,
            self.branch_id,
            resolve_active_leaf_id(numbered),
            tuple(entry.id for entry in batch),
            sequences,
            bool(batch),
        )

    async def fork(
        self, at_entry_id: str | None, *, token: RunToken, entries: Sequence[SessionEntry] = ()
    ) -> BranchHead:
        self._check_token(token)
        numbered = await self.read_all()
        known = {entry.id for entry in numbered}
        if at_entry_id is not None and at_entry_id not in known:
            raise SessionConflict("Unknown fork point")
        head = resolve_active_leaf_id(numbered)
        committed = await self._inner.compare_and_append(tuple(entries), head)
        self.branch_id = uuid4().hex
        return BranchHead(self.session_id, self.branch_id, resolve_active_leaf_id(committed))

    async def begin_run(self, run_id: str) -> RunToken:
        if self.closed:
            raise StaleRunToken("Session writer is closed")
        head = await self.get_head()
        self.token = RunToken(self.session_id, self._owner_id, run_id, self.token.generation + 1)
        self._run_start_heads[self.token.generation] = head.entry_id
        return self.token

    async def run_is_revoked(self) -> bool:
        return False

    def _fingerprint(self, outcome: RunOutcome) -> tuple[object, ...]:
        return (
            outcome.token.run_id,
            outcome.token.generation,
            outcome.status,
            outcome.expected_head,
            tuple(entry.id for entry in outcome.entries),
            outcome.branch_id,
            outcome.error,
            outcome.snapshot_id,
        )

    async def complete_run(self, outcome: RunOutcome) -> CompletionReceipt:
        previous = self._completions.get(outcome.token.generation)
        if previous is not None:
            if self._completion_keys.get(outcome.token.generation) != self._fingerprint(outcome):
                raise SessionConflict("Completion already recorded with a different outcome")
            return previous
        self._check_token(outcome.token)
        if outcome.branch_id != self.branch_id:
            raise SessionConflict("Run branch mismatch")
        if outcome.token.generation not in self._run_start_heads:
            raise StaleRunToken("Run start boundary is missing")

        run_entries = tuple(outcome.entries)
        end_entry_id = _batch_head(run_entries, fallback=outcome.expected_head)
        commit = RunCommitEntry(
            parent_id=end_entry_id,
            run_id=outcome.token.run_id,
            branch_id=outcome.branch_id,
            status=outcome.status,
            start_entry_id=self._run_start_heads[outcome.token.generation],
            end_entry_id=end_entry_id,
            snapshot_id=outcome.snapshot_id,
            error=outcome.error,
        )
        leaf = LeafEntry(parent_id=end_entry_id, entry_id=end_entry_id)
        numbered = await self._inner.compare_and_append(
            (*run_entries, commit, leaf), outcome.expected_head
        )
        receipt = CompletionReceipt(
            outcome.token.run_id,
            self.session_id,
            self.branch_id,
            outcome.status,
            resolve_active_leaf_id(numbered),
            len(numbered),
            outcome.snapshot_id,
        )
        self._completions[outcome.token.generation] = receipt
        self._completion_keys[outcome.token.generation] = self._fingerprint(outcome)
        self._run_start_heads.pop(outcome.token.generation, None)
        self.token = RunToken(
            self.session_id,
            self._owner_id,
            f"idle-{outcome.token.run_id}",
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
        del expected_head, builder_version
        self._check_token(token)
        snapshot_id = uuid4().hex
        self.snapshots[snapshot_id] = payload
        return snapshot_id

    async def get_snapshot(self, snapshot_id: str) -> dict[str, JSONValue]:
        payload = self.snapshots.get(snapshot_id)
        if payload is None:
            raise KeyError(snapshot_id)
        return {"payload": payload, "run_id": self.token.run_id, "snapshot_id": snapshot_id}

    async def aclose(self) -> None:
        self.closed = True


def _batch_head(entries: Sequence[SessionEntry], *, fallback: str | None) -> str | None:
    for entry in reversed(entries):
        if isinstance(entry, LeafEntry):
            return entry.entry_id
    for entry in reversed(entries):
        if not isinstance(entry, LeafEntry):
            return entry.id
    return fallback
