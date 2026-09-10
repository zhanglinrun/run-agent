"""Wakeable dispatcher over durable session heads; only active runs own coroutines."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from run_agent_gateway.contracts import Assignment, GatewayOwner
from run_agent_gateway.controller import SessionController
from run_agent_gateway.repository import GatewayRepository


class AssignmentRunner(Protocol):
    async def run(self, assignment: Assignment, cancellation: asyncio.Event) -> None:
        """Commit an outcome and exit all owned work before returning."""
        ...


@dataclass(slots=True)
class ActiveRun:
    assignment: Assignment
    cancellation: asyncio.Event
    task: asyncio.Task[None]


class GatewayScheduler:
    def __init__(
        self, repository: GatewayRepository, owner: GatewayOwner, runner: AssignmentRunner
    ) -> None:
        self.repository, self.owner, self.runner = repository, owner, runner
        self._wake = asyncio.Event()
        self._active: dict[str, ActiveRun] = {}
        self._dispatcher: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._closing = False
        self.failure: BaseException | None = None
        self.errors: list[str] = []

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start(self) -> None:
        if self._dispatcher is not None:
            raise RuntimeError("Gateway scheduler already started")
        self._dispatcher = asyncio.create_task(self._dispatch(), name="gateway-dispatch")
        self._heartbeat = asyncio.create_task(self._renew(), name="gateway-owner-lease")
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def signal_cancel(self, task_id: str) -> None:
        active = self._active.get(task_id)
        if active is not None:
            active.cancellation.set()
        self.wake()

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                await self.repository.renew(self.owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            self._closing = True
            for active in self._active.values():
                active.cancellation.set()
            self.wake()

    async def _dispatch(self) -> None:
        try:
            while not self._closing:
                self._wake.clear()
                await SessionController(self.repository).reconcile(self.owner)
                for task_id, active in tuple(self._active.items()):
                    state = await self.repository.task(
                        task_id, principal_id=active.assignment.principal_id
                    )
                    if state["status"] == "cancelling":
                        active.cancellation.set()
                while not self._closing:
                    assignment = await self.repository.claim_next(self.owner)
                    if assignment is None:
                        break
                    cancellation = asyncio.Event()
                    task = asyncio.create_task(
                        self._execute(assignment, cancellation),
                        name=f"gateway-run:{assignment.run_id}",
                    )
                    self._active[assignment.task_id] = ActiveRun(assignment, cancellation, task)
                try:
                    async with asyncio.timeout(0.25):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            self._closing = True
            for active in self._active.values():
                active.cancellation.set()

    async def _execute(self, assignment: Assignment, cancellation: asyncio.Event) -> None:
        try:
            await self.runner.run(assignment, cancellation)
            await self.repository.release(self.owner, assignment)
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.errors.append(message)
            del self.errors[:-64]
            try:
                await self.repository.contain(self.owner, assignment, error=message)
            except Exception as containment_error:
                self.failure = containment_error
                self._closing = True
        finally:
            self._active.pop(assignment.task_id, None)
            self.wake()

    async def shutdown(self, *, grace_period: float = 5, retain_lease: bool = False) -> None:
        self._closing = True
        self.wake()
        try:
            await self.repository.stop_accepting(self.owner)
            if self._dispatcher is not None:
                await self._dispatcher
            for active in tuple(self._active.values()):
                await self.repository.cancel(
                    self.owner,
                    active.assignment.task_id,
                    principal_id=active.assignment.principal_id,
                )
                active.cancellation.set()
            tasks = {active.task for active in self._active.values()}
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=max(0, grace_period))
                for task in pending:
                    task.cancel()
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=max(0.1, grace_period))
                if pending:
                    raise RuntimeError("Gateway runners have not exited; reservations retained")
        finally:
            if not retain_lease:
                await self.close_lease()

    async def close_lease(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.gather(self._heartbeat, return_exceptions=True)


__all__ = ["AssignmentRunner", "GatewayScheduler"]
