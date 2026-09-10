"""Source-owned asynchronous cleanup with bounded waiting and retained handles."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

Disposer = Callable[[], Awaitable[None]]


class DisposerOwner:
    def __init__(self, *, timeout: float = 1.0) -> None:
        self.timeout = timeout
        self._registered: dict[str, list[Disposer]] = {}
        self._retired: list[tuple[str, list[Disposer]]] = []
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
        owned.append(disposer)

    def retire_source(self, source_id: str) -> None:
        callbacks = self._registered.pop(source_id, [])
        if callbacks:
            self._retired.append((source_id, callbacks))

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

    async def _dispose(self, source_id: str, callbacks: list[Disposer]) -> None:
        # Reverse acquisition order within a source; failures do not skip siblings.
        for callback in reversed(callbacks):
            try:
                await callback()
            except asyncio.CancelledError:
                self.errors.append(f"{source_id}: cleanup callback was cancelled")
            except Exception as exc:
                self.errors.append(f"{source_id}: {type(exc).__name__}: {exc}")

    async def drain(self) -> int:
        for source_id, callbacks in self._retired:
            task = asyncio.create_task(
                self._dispose(source_id, callbacks), name=f"dispose:{source_id}"
            )
            self._tasks[task] = source_id
            task.add_done_callback(self._finished)
        self._retired.clear()
        if not self._tasks:
            return 0
        _, pending = await asyncio.wait(tuple(self._tasks), timeout=self.timeout)
        for task in pending:
            if task not in self._cancelled:
                self._cancelled.add(task)
                task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=min(0.05, self.timeout))
        for task in tuple(self._tasks):
            if task.done():
                self._finished(task)
        return len(self._tasks)
