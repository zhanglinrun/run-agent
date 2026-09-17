"""Bounded in-memory extension jobs. Interrupted work is not replayed."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from run_agent_coding.host.contracts import (
    ExtensionToken,
    HostServices,
    TaskContext,
    TaskHandler,
    TaskInfo,
    TaskSpec,
)
from run_agent_coding.storage.canonical import canonical_json
from run_agent_coding.storage.settle import settle
from run_agent_core.types import JSONValue

TASK_ORIGIN_KINDS = ("user", "review", "evaluation", "naming")


class TaskRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Job:
    task_id: str
    token: ExtensionToken
    spec: TaskSpec
    handler: TaskHandler
    services: HostServices
    assert_active: Callable[[], None]


class LocalTaskManager:
    """No coroutine per queued job, and no replay after process interruption."""

    def __init__(self, *, concurrency: int = 2, max_pending: int = 32) -> None:
        if concurrency < 1 or max_pending < 1:
            raise ValueError("Task limits must be positive")
        self.concurrency, self.max_pending = concurrency, max_pending
        self._pending: deque[_Job] = deque()
        self._jobs: dict[str, _Job] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._started: set[str] = set()
        self._cancelled: set[str] = set()
        self._records: dict[tuple[str, str, str], TaskInfo] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self.errors: list[str] = []

    def _key(self, token: ExtensionToken, task_id: str) -> tuple[str, str, str]:
        return token.session_id, token.source_id, task_id

    def _record(self, token: ExtensionToken, info: TaskInfo) -> None:
        self._records[self._key(token, info.task_id)] = info

    async def submit(
        self,
        token: ExtensionToken,
        spec: TaskSpec,
        handler: TaskHandler,
        services: HostServices,
        assert_active: Callable[[], None],
    ) -> str:
        assert_active()
        payload = json.loads(canonical_json(spec.payload))
        oversized = len(canonical_json(payload).encode()) > 65536
        if not spec.handler or len(spec.handler.encode()) > 128 or oversized:
            raise TaskRejected("Task handler or payload exceeds admission limits")
        if spec.origin_kind not in TASK_ORIGIN_KINDS:
            raise TaskRejected(f"Unknown task origin kind: {spec.origin_kind}")
        frozen = TaskSpec(
            spec.handler,
            payload,
            spec.snapshot_id,
            spec.origin_kind,
            spec.budget,
        )
        task_id = uuid4().hex

        async def admit() -> None:
            async with self._lock:
                assert_active()
                if self._closing or len(self._jobs) >= self.max_pending:
                    raise TaskRejected("Local background task capacity is full or closing")
                job = _Job(task_id, token, frozen, handler, services, assert_active)
                self._jobs[task_id] = job
                self._record(
                    token,
                    TaskInfo(
                        task_id,
                        frozen.handler,
                        "queued",
                        origin_kind=frozen.origin_kind,
                        budget=frozen.budget,
                    ),
                )
                self._pending.append(job)
                self._pump()

        _, cancelled = await settle(admit())
        if cancelled:
            await self.cancel(task_id)
            raise asyncio.CancelledError
        return task_id

    def _pump(self) -> None:
        while self._pending and len(self._running) < self.concurrency and not self._closing:
            job = self._pending.popleft()
            task = asyncio.create_task(self._run(job), name=f"extension-job:{job.task_id}")
            self._running[job.task_id] = task

    async def _run(self, job: _Job) -> None:
        self._started.add(job.task_id)
        result: JSONValue = None
        status, error = "failed", None
        try:
            if job.task_id in self._cancelled:
                raise asyncio.CancelledError
            job.assert_active()
            self._record(
                job.token,
                TaskInfo(
                    job.task_id,
                    job.spec.handler,
                    "running",
                    origin_kind=job.spec.origin_kind,
                    budget=job.spec.budget,
                ),
            )
            result = await job.handler(
                job.spec.payload,
                TaskContext(job.task_id, job.spec.snapshot_id, job.services),
            )
            job.assert_active()
            if len(canonical_json(result).encode()) > 65536:
                raise ValueError("Task result exceeds 64 KiB; save a referenced artifact instead")
            status = "cancelled" if job.task_id in self._cancelled else "succeeded"
        except asyncio.CancelledError:
            status, error = "cancelled", "Task cancelled"
        except Exception as exc:
            status = "cancelled" if job.task_id in self._cancelled else "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._record(
                job.token,
                TaskInfo(
                    job.task_id,
                    job.spec.handler,
                    status,
                    result,
                    error,
                    job.spec.origin_kind,
                    job.spec.budget,
                ),
            )
            self._running.pop(job.task_id, None)
            self._started.discard(job.task_id)
            self._jobs.pop(job.task_id, None)
            self._cancelled.discard(job.task_id)
            self._pump()

    async def status(self, token: ExtensionToken, task_id: str) -> TaskInfo:
        info = self._records.get(self._key(token, task_id))
        if info is None:
            raise KeyError("Unknown task in this extension session")
        return info

    async def cancel(self, task_id: str) -> None:
        job = self._jobs.get(task_id)
        if job is None:
            return
        self._cancelled.add(task_id)
        operation = self._running.get(task_id)
        if operation is None:
            if job in self._pending:
                self._pending.remove(job)
            self._record(
                job.token,
                TaskInfo(
                    job.task_id,
                    job.spec.handler,
                    "cancelled",
                    error="Cancelled before execution",
                    origin_kind=job.spec.origin_kind,
                    budget=job.spec.budget,
                ),
            )
            self._jobs.pop(task_id, None)
            self._cancelled.discard(task_id)
            return
        if task_id in self._started:
            operation.cancel()
        self._record(
            job.token,
            TaskInfo(
                job.task_id,
                job.spec.handler,
                "cancelling",
                origin_kind=job.spec.origin_kind,
                budget=job.spec.budget,
            ),
        )

    async def retire(self, session_id: str, generation: str, *, timeout: float = 1.0) -> int:
        matching = [
            job
            for job in self._jobs.values()
            if job.token.session_id == session_id and job.token.generation == generation
        ]
        errors: list[Exception] = []
        for job in matching:
            try:
                await self.cancel(job.task_id)
            except Exception as exc:
                errors.append(exc)
        running = [self._running[job.task_id] for job in matching if job.task_id in self._running]
        if not running:
            if errors:
                raise errors[0]
            return 0
        _, pending = await asyncio.wait(running, timeout=timeout)
        if errors:
            raise errors[0]
        return len(pending)

    async def aclose(self) -> None:
        self._closing = True
        generations = {(job.token.session_id, job.token.generation) for job in self._jobs.values()}
        for session_id, generation in generations:
            await self.retire(session_id, generation)
        if self._running:
            raise RuntimeError(f"{len(self._running)} extension tasks did not stop")
        if self.errors:
            raise RuntimeError("; ".join(self.errors))


class BoundTaskService:
    def __init__(
        self,
        manager: LocalTaskManager,
        token: ExtensionToken,
        handlers: dict[str, TaskHandler],
        services: HostServices,
        assert_active: Callable[[], None],
    ) -> None:
        self._manager, self._token, self._handlers = manager, token, handlers
        self._services, self._assert_active = services, assert_active

    async def submit(self, spec: TaskSpec) -> str:
        self._assert_active()
        if spec.handler not in self._handlers:
            raise TaskRejected("Task handler was not registered by this extension")
        return await self._manager.submit(
            self._token,
            spec,
            self._handlers[spec.handler],
            self._services,
            self._assert_active,
        )

    async def status(self, task_id: str) -> TaskInfo:
        self._assert_active()
        return await self._manager.status(self._token, task_id)

    async def cancel(self, task_id: str) -> TaskInfo:
        self._assert_active()
        await self._manager.status(self._token, task_id)
        await self._manager.cancel(task_id)
        return await self._manager.status(self._token, task_id)
