"""One named transaction for commits that span more than one domain.

Plan 7.4 requires a shared UnitOfWork at the composition layer, so that the
session's final events cannot commit separately from the gateway's terminal
state. The same abstraction serves the second composite point, where an
extension rebind and its activation entry commit together.

Participants apply in declaration order inside a single write transaction. The
transaction is the atomic boundary: every participant writes through the
supplied connection only, and none may await external work, because the unit
holds the connection for its whole duration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from run_agent_coding.storage.sqlite import SqliteDatabase


@dataclass(frozen=True, slots=True)
class CommitParticipant:
    """One domain's contribution to a composite commit."""

    name: str
    apply: Callable[[sqlite3.Connection], None]


class UnitOfWork:
    """Applies every participant of one composite commit atomically."""

    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    async def commit(self, *participants: CommitParticipant) -> None:
        """Apply all participants in order; a raise rolls the whole unit back."""
        if not participants:
            return

        def apply_all(connection: sqlite3.Connection) -> None:
            for participant in participants:
                participant.apply(connection)

        await self._database.run(apply_all, write=True)
