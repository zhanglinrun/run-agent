"""Per-session turn lease: one turn at a time per resolved coding session.

The adapter already serializes turns per chat key, but two chat keys can map to the same
coding session (a thread and its parent chat after a reset, or the same session reached
from two chats). Without a lease those two turns would interleave their writes on one
transcript. The lease serializes on the resolved session ID, hands out a generation-scoped
token so a stale release can never free a newer holder, and fails closed on timeout.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass


class TurnLeaseTimeoutError(TimeoutError):
    """The session is still busy after the wait budget; the turn must be rejected."""


@dataclass(frozen=True, slots=True)
class TurnLeaseToken:
    session_id: str
    holder: str
    generation: int


class _Lease:
    __slots__ = ("condition", "generation", "holder", "touched", "waiters")

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.holder: str | None = None
        self.generation = 0
        self.waiters = 0
        self.touched = time.monotonic()


class SessionTurnLeaseRegistry:
    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("Lease registry needs room for at least one session")
        self._leases: OrderedDict[str, _Lease] = OrderedDict()
        self._max_entries = max_entries

    def _lease(self, session_id: str) -> _Lease:
        lease = self._leases.get(session_id)
        if lease is None:
            self._evict_idle()
            lease = _Lease()
            self._leases[session_id] = lease
        self._leases.move_to_end(session_id)
        lease.touched = time.monotonic()
        return lease

    def _evict_idle(self) -> None:
        while len(self._leases) >= self._max_entries:
            idle = next(
                (k for k, v in self._leases.items() if v.holder is None and v.waiters == 0),
                None,
            )
            if idle is None:
                return
            del self._leases[idle]

    def is_held(self, session_id: str) -> bool:
        lease = self._leases.get(session_id)
        return lease is not None and lease.holder is not None

    def holder(self, session_id: str) -> str | None:
        lease = self._leases.get(session_id)
        return None if lease is None else lease.holder

    async def acquire(self, session_id: str, holder: str, *, timeout: float) -> TurnLeaseToken:
        lease = self._lease(session_id)
        lease.waiters += 1
        try:
            async with lease.condition:
                try:
                    async with asyncio.timeout(timeout):
                        await lease.condition.wait_for(lambda: lease.holder is None)
                except TimeoutError:
                    raise TurnLeaseTimeoutError(
                        f"session {session_id} is still busy (held by {lease.holder}) "
                        f"after {timeout:g}s"
                    ) from None
                lease.holder = holder
                lease.generation += 1
                return TurnLeaseToken(session_id, holder, lease.generation)
        finally:
            lease.waiters -= 1

    async def release(self, token: TurnLeaseToken) -> bool:
        """Free the lease only when this exact token still holds it."""
        lease = self._leases.get(token.session_id)
        if lease is None:
            return False
        async with lease.condition:
            if lease.holder != token.holder or lease.generation != token.generation:
                return False
            lease.holder = None
            lease.touched = time.monotonic()
            lease.condition.notify_all()
            return True


__all__ = ["SessionTurnLeaseRegistry", "TurnLeaseTimeoutError", "TurnLeaseToken"]
