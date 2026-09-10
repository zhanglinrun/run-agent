"""Execute assigned Coding runs, reconciling durable outcomes before releasing ownership."""

from __future__ import annotations

import asyncio

from run_agent_coding.application import CodingApplication
from run_agent_coding.storage.settle import settle
from run_agent_gateway.contracts import Assignment
from run_agent_gateway.runtime import GatewayCodingRuntime


class CodingAssignmentRunner:
    def __init__(self, runtime: GatewayCodingRuntime) -> None:
        self.runtime = runtime
        self.applications: dict[str, CodingApplication] = {}
        self.quarantined: dict[str, CodingApplication] = {}

    async def run(self, assignment: Assignment, cancellation: asyncio.Event) -> None:
        repository, owner = self.runtime.repository, self.runtime.owner
        application: CodingApplication | None = None
        consume: asyncio.Task[None] | None = None
        waiter: asyncio.Task[bool] | None = None
        error: BaseException | None = None
        try:
            if cancellation.is_set():
                await repository.complete(owner, assignment, status="cancelled")
                return
            application = await self.runtime.open(assignment)
            self.applications[assignment.run_id] = application

            async def prompt() -> None:
                assert application is not None
                async for _ in application.prompt(assignment.content, run_id=assignment.run_id):
                    pass

            consume = asyncio.create_task(prompt(), name=f"gateway-prompt:{assignment.run_id}")
            waiter = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait({consume, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done and not consume.done():
                application.session.cancel()
                consume.cancel()
            await consume
        except BaseException as exc:
            error = exc
        finally:
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if consume is not None and not consume.done():
                assert application is not None
                application.session.cancel()
                consume.cancel()

                async def drain() -> None:
                    await asyncio.gather(consume, return_exceptions=True)

                await settle(drain())
            self.applications.pop(assignment.run_id, None)
            if application is not None:
                try:
                    await application.aclose()
                except BaseException:
                    self.quarantined[assignment.run_id] = application
                    raise
                finally:
                    await application.manager.aclose()
        state = await repository.task(assignment.task_id, principal_id=assignment.principal_id)
        if state["status"] in {"succeeded", "failed", "cancelled"}:
            return
        # An input/setup error may precede begin_run. Once a Coding execution
        # exists, complete() refuses an outcome-free shortcut.
        status = "cancelled" if state["status"] == "cancelling" else "failed"
        await repository.complete(
            owner,
            assignment,
            status=status,
            error=(str(error) or type(error).__name__) if error else "Prompt produced no outcome",
        )


__all__ = ["CodingAssignmentRunner"]
