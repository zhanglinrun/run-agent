"""Bounded local extension jobs with durable descriptors and explicit shutdown."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from uuid import uuid4

from run_agent_coding.host.contracts import (
    ExtensionToken,
    HostServices,
    TaskBudget,
    TaskContext,
    TaskHandler,
    TaskInfo,
    TaskSpec,
)
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.settle import settle
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import assert_extension
from run_agent_core.types import JSONValue

# Mirrors the CHECK constraint on extension_tasks.origin_kind: the host assigns
# this, and auxiliary kinds are excluded from triggering further reviews.
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

    def __init__(
        self, database: SqliteDatabase, *, concurrency: int = 2, max_pending: int = 32
    ) -> None:
        if concurrency < 1 or max_pending < 1:
            raise ValueError("Task limits must be positive")
        self.database = database
        self.concurrency, self.max_pending = concurrency, max_pending
        self._pending: deque[_Job] = deque()
        self._jobs: dict[str, _Job] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._started: set[str] = set()
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()
        self._closing = False
        self.errors: list[str] = []

    async def submit(
        self,
        token: ExtensionToken,
        spec: TaskSpec,
        handler: TaskHandler,
        services: HostServices,
        assert_active: Callable[[], None],
    ) -> str:
        assert_active()
        payload = canonical_json(spec.payload)
        if not spec.handler or len(spec.handler.encode()) > 128 or len(payload.encode()) > 65536:
            raise TaskRejected("Task handler or payload exceeds admission limits")
        if spec.origin_kind not in TASK_ORIGIN_KINDS:
            raise TaskRejected(f"Unknown task origin kind: {spec.origin_kind}")
        frozen = TaskSpec(
            spec.handler,
            json.loads(payload),
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

                def persist(connection: sqlite3.Connection) -> None:
                    assert_active()
                    assert_extension(connection, token)
                    if frozen.snapshot_id is not None:
                        snapshot = connection.execute(
                            "SELECT 1 FROM context_snapshots WHERE snapshot_id=? AND session_id=?",
                            (frozen.snapshot_id, token.session_id),
                        ).fetchone()
                        if snapshot is None:
                            raise TaskRejected(
                                "Task snapshot is missing or belongs to another session"
                            )
                    connection.execute(
                        "INSERT INTO extension_tasks "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'queued',NULL,NULL,?,NULL)",
                        (
                            task_id,
                            token.session_id,
                            token.source_id,
                            token.owner_id,
                            token.generation,
                            frozen.handler,
                            payload,
                            frozen.snapshot_id,
                            frozen.origin_kind,
                            canonical_json(frozen.budget.as_json()),
                            time(),
                        ),
                    )

                await self.database.run(persist, write=True)
                job = _Job(task_id, token, frozen, handler, services, assert_active)
                self._jobs[task_id] = job
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
            await self._set_status(job, "running")
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
            try:
                await settle(self._set_status(job, status, result=result, error=error))
            except Exception as exc:
                self.errors.append(f"Task {job.task_id} completion: {type(exc).__name__}: {exc}")
                self.errors[:] = self.errors[-32:]
            self._running.pop(job.task_id, None)
            self._started.discard(job.task_id)
            self._jobs.pop(job.task_id, None)
            self._cancelled.discard(job.task_id)
            self._pump()

    async def _set_status(
        self,
        job: _Job,
        status: str,
        *,
        result: JSONValue = None,
        error: str | None = None,
    ) -> None:
        token = job.token
        encoded = canonical_json(result)

        def update(connection: sqlite3.Connection) -> None:
            # Task cleanup can record its own outcome after extension retirement,
            # but a process which lost session ownership cannot publish success.
            current = connection.execute(
                "SELECT owner_id,owner_active FROM sessions WHERE session_id=?",
                (token.session_id,),
            ).fetchone()
            selected = status
            if status == "succeeded":
                try:
                    job.assert_active()
                    assert_extension(connection, token)
                except Exception:
                    selected = "interrupted"
                if current is None or current[0] != token.owner_id or not current[1]:
                    selected = "interrupted"
            terminal = selected in {"succeeded", "failed", "cancelled", "interrupted"}
            connection.execute(
                "UPDATE extension_tasks SET status=?,result_json=?,error=?,finished_at=? "
                "WHERE task_id=? AND owner_id=? AND generation=? "
                "AND status IN ('queued','running','cancelling')",
                (
                    selected,
                    encoded,
                    error,
                    time() if terminal else None,
                    job.task_id,
                    token.owner_id,
                    token.generation,
                ),
            )

        await self.database.run(update, write=True)

    async def status(self, token: ExtensionToken, task_id: str) -> TaskInfo:
        def read(connection: sqlite3.Connection) -> TaskInfo:
            row = connection.execute(
                "SELECT handler,status,result_json,error,origin_kind,budget_json "
                "FROM extension_tasks "
                "WHERE task_id=? AND session_id=? AND source_id=?",
                (task_id, token.session_id, token.source_id),
            ).fetchone()
            if row is None:
                raise KeyError("Unknown task in this extension session")
            return TaskInfo(
                task_id,
                row[0],
                row[1],
                json.loads(row[2]) if row[2] else None,
                row[3],
                row[4],
                TaskBudget.from_json(json.loads(row[5])),
            )

        return await self.database.run(read)

    async def cancel(self, task_id: str) -> None:
        job = self._jobs.get(task_id)
        if job is None:
            return
        self._cancelled.add(task_id)
        operation = self._running.get(task_id)
        if operation is None:
            self._pending.remove(job)
            try:
                await self._set_status(job, "cancelled", error="Cancelled before execution")
            finally:
                self._jobs.pop(task_id, None)
                self._cancelled.discard(task_id)
        else:
            if task_id in self._started:
                operation.cancel()
            await self._set_status(job, "cancelling")

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
