"""Source-owned asynchronous cleanup with a global reverse-order drain."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

Disposer = Callable[[], Awaitable[None]]


class DisposerOwner:
    """Owns the async cleanup callbacks of every source in one generation.

    Drain order is the global reverse of registration order, across sources:
    the callback registered last runs first even when its source retires before
    an older one. Each callback runs at most once, a failing callback never
    skips the remaining ones, and repeated drains only pick up callbacks that
    were not retired yet or are still owned after bounded waiting.
    """

    def __init__(self, *, timeout: float = 1.0) -> None:
        self.timeout = timeout
        self._registered: dict[str, list[tuple[int, Disposer]]] = {}
        self._retired: list[tuple[int, str, Disposer]] = []
        self._next_sequence = 0
        self._tasks: dict[asyncio.Task[None], str] = {}
        self._cancelled: set[asyncio.Task[None]] = set()
        self._closed = False
        self.errors: list[str] = []

    def register(self, source_id: str, disposer: Disposer) -> None:
        if self._closed:
            raise RuntimeError("Cleanup owner is retired")
        if not inspect.iscoroutinefunction(disposer):
            raise TypeError("Disposers must be async functions")
        owned = self._registered.setdefault(source_id, [])
        if len(owned) >= 64:
            raise ValueError("An extension can own at most 64 disposers")
        owned.append((self._next_sequence, disposer))
        self._next_sequence += 1

    def retire_source(self, source_id: str) -> None:
        callbacks = self._registered.pop(source_id, [])
        for sequence, disposer in callbacks:
            self._retired.append((sequence, source_id, disposer))

    def retire(self) -> None:
        self._closed = True
        for source_id in tuple(self._registered):
            self.retire_source(source_id)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task not in self._tasks:
            return
        source = self._tasks.pop(task)
        self._cancelled.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.errors.append(f"{source}: {type(error).__name__}: {error}")

    async def _dispose(self, source_id: str, callback: Disposer) -> None:
        # One callback per task: failures and cancellation are recorded here and
        # the remaining callbacks still run, in order, on later iterations.
        try:
            await callback()
        except asyncio.CancelledError:
            self.errors.append(f"{source_id}: cleanup callback was cancelled")
        except Exception as exc:
            self.errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    def _pop_next(self) -> tuple[str, Disposer] | None:
        """Return the latest registered retired callback, or None."""
        if not self._retired:
            return None
        position = max(range(len(self._retired)), key=lambda index: self._retired[index][0])
        _sequence, source_id, disposer = self._retired.pop(position)
        return source_id, disposer

    def _settle_done(self) -> None:
        for task in tuple(self._tasks):
            if task.done():
                self._finished(task)

    async def _settle_owned(self) -> None:
        """Await tasks an earlier bounded drain kept owned."""
        await asyncio.wait(tuple(self._tasks), timeout=self.timeout)
        self._settle_done()

    async def drain(self) -> int:
        """Run every retired callback once, latest registration first.

        A callback that does not finish inside `timeout` is cancelled and kept
        owned; drain then stops before starting older callbacks so the global
        reverse order holds across drains. Returns the number of owned tasks.
        """
        if self._tasks:
            await self._settle_owned()
        if self._tasks:
            return len(self._tasks)
        while (entry := self._pop_next()) is not None:
            source_id, callback = entry
            task = asyncio.create_task(
                self._dispose(source_id, callback), name=f"dispose:{source_id}"
            )
            self._tasks[task] = source_id
            task.add_done_callback(self._finished)
            done, _ = await asyncio.wait((task,), timeout=self.timeout)
            if not done:
                self._cancelled.add(task)
                task.cancel()
                done, _ = await asyncio.wait((task,), timeout=min(0.05, self.timeout))
                if not done:
                    break
            self._finished(task)
        self._settle_done()
        return len(self._tasks)
