"""Open a bounded, assignment-owned Coding application on the shared database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.provider_config import ProviderSettings
from run_agent_coding.session_manager import SessionManager
from run_agent_core.provider import ModelProvider
from run_agent_gateway.contracts import Assignment, GatewayOwner
from run_agent_gateway.repository import GatewayRepository


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

    async def open(self, assignment: Assignment) -> CodingApplication:
        manager = SessionManager(
            self.options.paths,
            database=self.repository.database,
            principal_id=assignment.principal_id,
            owner_id=self.owner.owner_id,
        )
        try:
            return await CodingApplication.open(
                replace(self.options, cwd=assignment.workspace, resume=assignment.session_id),
                manager=manager,
                settings=self.settings,
                provider=self.provider_factory(assignment) if self.provider_factory else None,
                committer=self.repository.committer(self.owner, assignment),
                input_source=self.repository.input_source(self.owner, assignment),
            )
        except BaseException:
            await manager.aclose()
            raise


__all__ = ["GatewayCodingRuntime"]
