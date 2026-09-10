"""Evaluation executor using the same durable application lifecycle as ``run``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.host.learning import writeback_disabled
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault
from run_agent_coding.provider_config import ProviderSettings, load_provider_settings
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.session_usage import estimated_request_cost
from run_agent_coding.storage.settle import settle
from run_agent_coding.thinking import ThinkingLevel
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage
from run_agent_core.provider import ModelProvider
from run_agent_evals.models import ExecutionCancelled, ExecutionFailure, ExecutionResult, FrozenTask
from run_agent_observability import ProviderCallLedger, summarize_provider_calls


class CodingTaskExecutor:
    def __init__(
        self,
        state_root: str | Path,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        provider_settings: ProviderSettings | None = None,
        thinking_level_override: ThinkingLevel | None = None,
        extension_paths: tuple[Path, ...] = (),
        project_extensions_enabled: bool = False,
        trust_default: TrustDefault = "never",
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.paths = RunAgentPaths(
            home=self.state_root,
            agents_home=self.state_root / ".agents",
        )
        self.provider_name = provider_name
        self.model = model
        self.provider_settings = provider_settings or load_provider_settings()
        self.thinking_level_override = thinking_level_override
        self.extension_paths = extension_paths
        self.project_extensions_enabled = project_extensions_enabled
        self.trust_default = trust_default

    async def execute(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        """Run one measured trial with learning writeback switched off.

        An evaluation must not change the experience it is measuring, so the guard
        is set for the whole trial and restored afterwards.
        """
        with writeback_disabled():
            return await self._run_measured(task, workspace)

    async def _run_measured(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        call_id = uuid4().hex
        session_id = f"eval-{call_id}"
        manager = SessionManager(self.paths)
        ledger: ProviderCallLedger | None = None
        application: CodingApplication | None = None
        final: AssistantMessage | None = None
        failure: BaseException | None = None
        records: list[dict[str, Any]] = []
        cleanup_errors: list[str] = []
        provider_name, model = self.provider_name, self.model
        try:
            ledger = ProviderCallLedger(
                await manager.telemetry(),
                stream=f"calls:{call_id}",
                root_id=call_id,
                session_id=session_id,
            )

            def instrument(provider: ModelProvider, name: str) -> ModelProvider:
                assert ledger is not None
                return ledger.instrument(provider, provider_name=name)

            application = await CodingApplication.open(
                ApplicationOptions(
                    cwd=workspace,
                    paths=self.paths,
                    session_id=session_id,
                    provider_name=self.provider_name,
                    model=self.model,
                    thinking=self.thinking_level_override,
                    extension_paths=self.extension_paths,
                    project_extensions_enabled=self.project_extensions_enabled,
                    trust_default=self.trust_default,
                ),
                manager=manager,
                settings=self.provider_settings,
                provider_transform=instrument,
            )
            provider_name, model = application.session.provider_name, application.session.model
            async for event in application.prompt(task.prompt):
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    final = event.message
            if final is None:
                raise RuntimeError("coding session produced no assistant response")
            if final.stop_reason in {"error", "aborted"}:
                raise RuntimeError(
                    final.error_message or f"assistant stopped with {final.stop_reason}"
                )
        except (Exception, asyncio.CancelledError) as exc:
            failure = exc
        finally:

            async def finish() -> None:
                nonlocal records
                try:
                    if application is not None:
                        await application.aclose()
                except Exception as exc:
                    cleanup_errors.append(f"Application close: {type(exc).__name__}: {exc}")
                try:
                    if ledger is not None:
                        records = await ledger.read_all()
                except Exception as exc:
                    cleanup_errors.append(f"Accounting flush: {type(exc).__name__}: {exc}")
                finally:
                    if ledger is not None:
                        ledger.close()
                    try:
                        await manager.aclose()
                    except Exception as exc:
                        cleanup_errors.append(f"Storage close: {type(exc).__name__}: {exc}")

            _, cancelled = await settle(finish())
            if cancelled:
                failure = asyncio.CancelledError()
        if cleanup_errors and failure is None:
            failure = RuntimeError("; ".join(cleanup_errors))
        efficiency = summarize_provider_calls(records)
        accounting_complete = ledger is not None and ledger.complete and not cleanup_errors
        calls = [record for record in records if record.get("type") == "provider_call"]
        estimates = [_call_cost(record) for record in calls]
        known_cost = sum(cost for cost, _ in estimates if cost is not None)
        cost_complete = accounting_complete and all(cost is not None for cost, _ in estimates)
        sources = {source for _, source in estimates}
        result = ExecutionResult(
            output=final.text if final else "",
            metadata={
                "calls": efficiency.logical_calls,
                "physical_attempts": efficiency.physical_attempts,
                "retries": efficiency.retry_count,
                "input_tokens": efficiency.input_tokens,
                "output_tokens": efficiency.output_tokens,
                "cache_read_tokens": efficiency.cache_read_tokens,
                "cache_write_tokens": efficiency.cache_write_tokens,
                "cache_write_1h_tokens": efficiency.cache_write_1h_tokens,
                "cost": known_cost if cost_complete else None,
                "known_cost": known_cost,
                "cost_source": next(iter(sources)) if len(sources) == 1 else "mixed",
                "accounting_complete": accounting_complete,
                "cost_complete": cost_complete,
                "provider": provider_name,
                "model": model,
                "session_id": session_id,
                "root_id": call_id,
                "database": str(self.paths.database_path),
                "call_ledger": str(self.paths.database_path),
                "call_stream": f"calls:{call_id}",
                "cleanup_errors": list(cleanup_errors),
            },
        )
        if isinstance(failure, asyncio.CancelledError):
            raise ExecutionCancelled(result) from failure
        if failure is not None:
            raise ExecutionFailure(str(failure) or type(failure).__name__, result) from failure
        return result


def _call_cost(record: dict[str, Any]) -> tuple[float | None, str]:
    if not record.get("usage_observed"):
        return None, "unavailable"
    reported = float(record.get("cost", 0.0))
    if reported > 0:
        return reported, "provider_reported"
    estimate = estimated_request_cost(
        str(record.get("provider", "")),
        str(record.get("model", "")),
        fresh=int(record.get("input_tokens", 0)),
        cached=int(record.get("cache_read_tokens", 0)),
        cache_write=int(record.get("cache_write_tokens", 0)),
        cache_write_1h=int(record.get("cache_write_1h_tokens", 0)),
        output=int(record.get("output_tokens", 0)),
    )
    return estimate, "catalog_estimate" if estimate is not None else "unavailable"


__all__ = ["CodingTaskExecutor"]
