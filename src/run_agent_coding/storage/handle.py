"""A session lifetime's bound SQLite writer; hosts own the database itself."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from uuid import uuid4

from run_agent_coding.storage.handle_runs import RunLifecycle
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.settle import settle
from run_agent_core.session.contracts import (
    AppendReceipt,
    BranchHead,
    CompletionReceipt,
    EntryPage,
    RunOutcome,
    RunToken,
    StaleRunToken,
)
from run_agent_core.session.entries import SessionEntry
from run_agent_core.types import JSONValue

OutcomeCommitter = Callable[[RunOutcome], Awaitable[CompletionReceipt]]


class SqliteSessionHandle(RunLifecycle):
    def __init__(
        self,
        repository: SqliteSessionRepository,
        token: RunToken,
        branch_id: str,
        *,
        committer: OutcomeCommitter | None = None,
    ) -> None:
        self.repository = repository
        self.token = token
        self.session_id = token.session_id
        self.branch_id = branch_id
        self._committer = committer or repository.complete_run
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._qualification_lock = asyncio.Lock()
        self._heartbeat_error: BaseException | None = None
        self._heartbeat = asyncio.create_task(
            self._renew(), name=f"session-lease:{self.session_id}"
        )

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                async with self._qualification_lock:
                    await self.repository.renew(self.token, ttl_seconds=120)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._heartbeat_error = exc

    def _check(self) -> None:
        if self._closed or self._heartbeat_error is not None:
            raise StaleRunToken("Session writer is closed or its ownership lease was lost")

    @property
    def closed(self) -> bool:
        return self._closed

    async def read_entries(self, *, after_seq: int = 0, limit: int = 1000) -> EntryPage:
        self._check()
        return await self.repository.read_entries(self.session_id, after_seq=after_seq, limit=limit)

    async def get_head(self) -> BranchHead:
        self._check()
        return await self.repository.get_head(self.session_id, self.branch_id)

    async def append_entries(
        self, entries: Sequence[SessionEntry], *, expected_head: str | None, token: RunToken
    ) -> AppendReceipt:
        self._check()
        return await self.repository.append_entries(
            entries, token=token, branch_id=self.branch_id, expected_head=expected_head
        )

    async def fork(
        self, at_entry_id: str | None, *, token: RunToken, entries: Sequence[SessionEntry] = ()
    ) -> BranchHead:
        self._check()
        branch_id = uuid4().hex

        frozen = tuple(entry.model_copy(deep=True) for entry in entries)

        def fork(connection: sqlite3.Connection) -> BranchHead:
            self._assert_branch_point(connection, token, at_entry_id)
            self._record_branch(connection, branch_id, at_entry_id)
            receipt = self.repository.append_in_transaction(
                connection, frozen, token=token, branch_id=branch_id, expected_head=at_entry_id
            )
            return BranchHead(self.session_id, branch_id, receipt.head_id)

        head, _ = await settle(self.repository.database.run(fork, write=True))
        self.branch_id = branch_id
        return head

    def _assert_branch_point(
        self, connection: sqlite3.Connection, token: RunToken, at_entry_id: str | None
    ) -> None:
        """The run token must still hold and the fork point must be a real entry."""
        self.repository.assert_token(connection, token)
        if at_entry_id is None:
            return
        known = connection.execute(
            "SELECT 1 FROM entries WHERE session_id=? AND entry_id=?",
            (self.session_id, at_entry_id),
        ).fetchone()
        if known is None:
            raise KeyError(f"Unknown branch point: {at_entry_id}")

    def _record_branch(
        self, connection: sqlite3.Connection, branch_id: str, at_entry_id: str | None
    ) -> None:
        """Register the new branch, then make it the session's active one."""
        connection.execute(
            """INSERT INTO branches(session_id,branch_id,parent_branch_id,
               fork_entry_id,head_id,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                self.session_id,
                branch_id,
                self.branch_id,
                at_entry_id,
                at_entry_id,
                self.repository.clock(),
            ),
        )
        connection.execute(
            "UPDATE sessions SET active_branch_id=? WHERE session_id=?",
            (branch_id, self.session_id),
        )

    async def record_context(
        self,
        payload: dict[str, JSONValue],
        *,
        token: RunToken,
        expected_head: str | None,
        builder_version: str,
    ) -> str:
        self._check()
        return await self.repository.put_snapshot(
            token=token,
            branch_id=self.branch_id,
            expected_head=expected_head,
            builder_version=builder_version,
            payload=payload,
        )

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())

        async def close() -> None:
            assert self._close_task is not None
            await self._close_task

        _, cancelled = await settle(close())
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await self._heartbeat
        async with self._qualification_lock:
            with suppress(StaleRunToken):
                await self.repository.release(self.token)
