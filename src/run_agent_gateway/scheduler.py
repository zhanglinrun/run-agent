"""Session-aware foreground/background turn scheduler."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
from time import monotonic
from typing import Protocol

from run_agent_gateway.models import TurnRequest, TurnResult


class TurnRunner(Protocol):
    async def run(self, request: TurnRequest, cancellation: asyncio.Event) -> TurnResult:
        """Execute one admitted turn."""
        ...


class SchedulerClosedError(RuntimeError):
    pass


class SchedulerOverloadedError(RuntimeError):
    pass


class TurnHandle:
    """Cancellation and result handle for one submitted request."""

    def __init__(
        self,
        request: TurnRequest,
        cancellation: asyncio.Event,
        task: asyncio.Task[TurnResult],
    ) -> None:
        self.request = request
        self._cancellation = cancellation
        self._task = task

    @property
    def done(self) -> bool:
        return self._task.done()

    def cancel(self) -> None:
        self._cancellation.set()

    async def result(self) -> TurnResult:
        return await asyncio.shield(self._task)


class TurnScheduler:
    """Bounded scheduler with per-session FIFO and isolated lane capacity."""

    def __init__(
        self,
        runner: TurnRunner,
        *,
        foreground_limit: int = 4,
        background_limit: int = 1,
        max_queued: int = 256,
    ) -> None:
        if foreground_limit < 1 or background_limit < 1:
            raise ValueError("scheduler lane limits must be at least 1")
        if max_queued < foreground_limit + background_limit:
            raise ValueError("max_queued must cover configured running capacity")
        self._loop = asyncio.get_running_loop()
        self._runner = runner
        self._lane_slots = {
            "foreground": asyncio.Semaphore(foreground_limit),
            "background": asyncio.Semaphore(background_limit),
        }
        self._max_queued = max_queued
        self._closed = False
        self._handles: dict[str, TurnHandle] = {}
        self._session_tails: dict[str, asyncio.Future[None]] = {}
        self._active_count = 0
        self._accepted_count = 0
        self._completed_count = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def accepted_count(self) -> int:
        return self._accepted_count

    @property
    def completed_count(self) -> int:
        return self._completed_count

    def submit(self, request: TurnRequest) -> TurnHandle:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("submit must be called from the scheduler's event loop")
        if self._closed:
            raise SchedulerClosedError("turn scheduler is closed")
        if request.id in self._handles:
            raise ValueError(f"duplicate turn request id: {request.id}")
        if self.active_count >= self._max_queued:
            raise SchedulerOverloadedError(
                f"turn scheduler has reached max_queued={self._max_queued}"
            )

        loop = asyncio.get_running_loop()
        predecessor = self._session_tails.get(request.session_id)
        completion = loop.create_future()
        self._session_tails[request.session_id] = completion
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            self._execute(request, cancellation, predecessor, completion),
            name=f"run-agent-turn:{request.id}",
        )
        handle = TurnHandle(request, cancellation, task)
        self._handles[request.id] = handle
        self._active_count += 1
        self._accepted_count += 1
        task.add_done_callback(partial(self._discard_finished, request.id))
        return handle

    async def run(self, request: TurnRequest) -> TurnResult:
        return await self.submit(request).result()

    async def shutdown(self, *, grace_period: float = 5.0) -> None:
        """Stop admission and drain; cancel remaining turns after the grace period."""
        self._closed = True
        tasks = [handle._task for handle in self._handles.values() if not handle.done]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=max(0.0, grace_period))
        del done
        if not pending:
            return
        for handle in self._handles.values():
            if not handle.done:
                handle.cancel()
        done, pending = await asyncio.wait(pending, timeout=max(0.1, grace_period))
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _execute(
        self,
        request: TurnRequest,
        cancellation: asyncio.Event,
        predecessor: asyncio.Future[None] | None,
        completion: asyncio.Future[None],
    ) -> TurnResult:
        started_at: float | None = None
        slot_acquired = False
        slot = self._lane_slots[request.lane]
        try:
            if predecessor is not None:
                await predecessor
            if cancellation.is_set():
                return TurnResult.cancelled(request)
            slot_acquired = await _acquire_or_cancel(slot, cancellation)
            if not slot_acquired:
                return TurnResult.cancelled(request)
            started_at = monotonic()
            result = await self._runner.run(request, cancellation)
            if result.started_at is None:
                result = TurnResult(
                    request_id=result.request_id,
                    session_id=result.session_id,
                    status=result.status,
                    output=result.output,
                    error=result.error,
                    submitted_at=result.submitted_at or request.submitted_at,
                    started_at=started_at,
                    finished_at=result.finished_at,
                    metadata=result.metadata,
                )
            return result
        except asyncio.CancelledError:
            cancellation.set()
            return TurnResult.cancelled(request, started_at=started_at)
        except Exception as exc:  # noqa: BLE001 - runner is a host boundary
            return TurnResult.failed(
                request,
                error=str(exc) or type(exc).__name__,
                started_at=started_at,
            )
        finally:
            if slot_acquired:
                slot.release()
            if not completion.done():
                completion.set_result(None)
            if self._session_tails.get(request.session_id) is completion:
                self._session_tails.pop(request.session_id, None)

    def _discard_finished(
        self,
        request_id: str,
        task: asyncio.Task[TurnResult],
    ) -> None:
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        self._active_count -= 1
        self._completed_count += 1
        self._handles.pop(request_id, None)


async def _acquire_or_cancel(
    semaphore: asyncio.Semaphore,
    cancellation: asyncio.Event,
) -> bool:
    acquire = asyncio.create_task(semaphore.acquire())
    cancelled = asyncio.create_task(cancellation.wait())
    try:
        done, pending = await asyncio.wait(
            {acquire, cancelled},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done and cancellation.is_set():
            if acquire in done and acquire.result():
                semaphore.release()
            return False
        return bool(acquire.result())
    finally:
        for task in (acquire, cancelled):
            if not task.done():
                task.cancel()


__all__ = [
    "SchedulerClosedError",
    "SchedulerOverloadedError",
    "TurnHandle",
    "TurnRunner",
    "TurnScheduler",
]
