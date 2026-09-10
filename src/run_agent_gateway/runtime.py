"""Open a bounded, assignment-owned Coding application on the shared database."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.provider_config import ProviderSettings
from run_agent_coding.session_manager import SessionManager
from run_agent_core.provider import ModelProvider
from run_agent_core.session.contracts import CompletionReceipt, RunOutcome
from run_agent_gateway.contracts import Assignment, GatewayOwner, Submission
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.workspaces import decode_revision


class GatewayCodingRuntime:
    def __init__(
        self,
        repository: GatewayRepository,
        owner: GatewayOwner,
        options: ApplicationOptions,
        *,
        settings: ProviderSettings | None = None,
        provider_factory: Callable[[Assignment], ModelProvider] | None = None,
    ) -> None:
        self.repository, self.owner, self.options = repository, owner, options
        self.settings, self.provider_factory = settings, provider_factory
        self._preparation_lock = asyncio.Lock()

    async def open(self, assignment: Assignment) -> CodingApplication:
        if assignment.lane == "background":
            state = await self.repository.task(
                assignment.task_id, principal_id=assignment.principal_id
            )
            await self.repository.workspaces.materialize(
                assignment.task_id, decode_revision(state["revision_json"])
            )
        manager = SessionManager(
            self.options.paths,
            database=self.repository.database,
            principal_id=assignment.principal_id,
            owner_id=self.owner.owner_id,
        )
        commit = self.repository.committer(self.owner, assignment)

        async def complete(outcome: RunOutcome) -> CompletionReceipt:
            if assignment.lane == "background":
                state = await self.repository.task(
                    assignment.task_id, principal_id=assignment.principal_id
                )
                report = await self.repository.workspaces.collect(
                    assignment.task_id, decode_revision(state["revision_json"])
                )
                await self.repository.record_background_artifacts(self.owner, assignment, report)
            return await commit(outcome)

        try:
            return await CodingApplication.open(
                replace(
                    self.options,
                    cwd=assignment.workspace,
                    resume=assignment.session_id,
                    pinned_resources=assignment.lane == "background",
                ),
                manager=manager,
                settings=self.settings,
                provider=self.provider_factory(assignment) if self.provider_factory else None,
                committer=complete,
                input_source=self.repository.input_source(self.owner, assignment),
            )
        except BaseException:
            await manager.aclose()
            raise

    async def prepare_background(self, submission: Submission) -> None:
        async with self._preparation_lock:
            await self._prepare_background(submission)

    async def _prepare_background(self, submission: Submission) -> None:
        # Re-delivery must not depend on the source still being clean or existing.
        known = await self.repository.database.run(
            lambda connection: connection.execute(
                "SELECT 1 FROM gateway_inbox WHERE adapter_instance_id=? AND source_message_id=?",
                (submission.route.adapter_instance_id, submission.source_message_id),
            ).fetchone()
        )
        if known is not None:
            return
        await self.repository.workspaces.capture(submission.workspace)
        session_id = await self.repository.background_source(
            self.owner,
            submission,
            model=self.options.model or "",
            provider_name=self.options.provider_name,
        )
        if session_id is None:
            return
        manager = SessionManager(
            self.options.paths,
            database=self.repository.database,
            principal_id=submission.principal_id,
            owner_id=self.owner.owner_id,
        )
        try:
            async with await CodingApplication.open(
                replace(self.options, cwd=submission.workspace, resume=session_id),
                manager=manager,
                settings=self.settings,
            ) as application:
                await application.start()
        finally:
            await manager.aclose()


__all__ = ["GatewayCodingRuntime"]
