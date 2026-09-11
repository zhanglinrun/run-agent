"""The execution lease half of a SQLite session handle.

Extracted from ``handle`` because the two answer different questions. A handle owns a
session's data - entries, heads, branches, snapshots - while this file is about who holds
the right to write: acquiring a run token, checking whether that token has been revoked,
and giving it up on completion. Together they crossed 200 lines.

A mixin rather than a collaborator object because every method here operates on the
handle's own token and qualification lock. Passing those in would have meant an
eight-argument helper purely to avoid a base class.

The host class provides ``_check``, and injects ``_committer``. The stub and the
annotation below exist so the type checker knows both names; the concrete class's own
definitions are what actually run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.settle import settle
from run_agent_core.session.contracts import CompletionReceipt, RunOutcome, RunToken


class RunLifecycle:
    """Acquiring, testing and releasing the right to write for one execution."""

    session_id: str
    branch_id: str
    token: RunToken
    repository: SqliteSessionRepository
    _qualification_lock: asyncio.Lock
    # Injected by the handle's constructor, not defined here: an annotation rather than a
    # stub, because assigning over a method body is what the type checker rejects.
    _committer: Callable[[RunOutcome], Awaitable[CompletionReceipt]]

    def _check(self) -> None:
        raise NotImplementedError

    async def begin_run(self, run_id: str) -> RunToken:
        self._check()
        async with self._qualification_lock:
            self.token, cancelled = await settle(
                self.repository.begin_run(self.token, branch_id=self.branch_id, run_id=run_id)
            )
            if cancelled:
                raise asyncio.CancelledError
            return self.token

    async def run_is_revoked(self) -> bool:
        """Whether the host revoked this execution, without restoring write authority."""
        self._check()
        run_id = self.token.run_id
        return await self.repository.database.run(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM execution_revocations WHERE run_id=?", (run_id,)
                ).fetchone()
                is not None
            )
        )

    async def complete_run(self, outcome: RunOutcome) -> CompletionReceipt:
        self._check()

        async def commit() -> CompletionReceipt:
            return await self._committer(outcome)

        async with self._qualification_lock:
            receipt, _ = await settle(commit())
            if self.token == outcome.token:
                self.token = self._next_idle_token()
            return receipt

    def _next_idle_token(self) -> RunToken:
        """The same owner and run, one generation on, flagged idle."""
        return RunToken(
            self.session_id,
            self.token.owner_id,
            f"idle-{self.token.run_id}",
            self.token.generation + 1,
        )
