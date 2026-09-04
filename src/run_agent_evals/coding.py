"""Real CodingSession executor for evaluation campaigns."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault
from run_agent_coding.provider_config import (
    ProviderSettings,
    load_provider_settings,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from run_agent_coding.provider_runtime import create_model_provider
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_coding.session import CodingSession, CodingSessionConfig
from run_agent_coding.session_usage import estimated_request_cost
from run_agent_coding.shell_config import load_shell_settings
from run_agent_coding.thinking import ThinkingLevel
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage
from run_agent_core.session import InMemorySessionStorage
from run_agent_evals.models import ExecutionResult, FrozenTask
from run_agent_observability import ProviderCallLedger, summarize_provider_calls


class CodingTaskExecutor:
    """Execute a frozen task through the same CodingSession used by the CLI and gateway."""

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
        selection = resolve_provider_selection(
            self.provider_settings,
            provider_name=self.provider_name,
            model=self.model,
        )
        thinking_level = resolve_startup_thinking_level(
            selection.provider,
            selection.model,
            cli_override=self.thinking_level_override,
        )
        raw_provider = create_model_provider(
            selection.provider,
            model=selection.model,
            thinking_level=thinking_level,
        )
        call_id = uuid4().hex
        ledger = ProviderCallLedger(self.state_root / "calls" / f"{call_id}.jsonl")
        provider = ledger.instrument(raw_provider, provider_name=selection.provider.name)
        resource_paths = RunAgentResourcePaths(
            root=self.state_root,
            cwd=workspace,
            agents_root=None,
            paths=self.paths,
            project_resources_enabled=self.trust_default == "always",
        )
        shell = load_shell_settings()
        session: CodingSession | None = None
        final: AssistantMessage | None = None
        try:
            session = await CodingSession.load(
                CodingSessionConfig(
                    provider=provider,
                    owns_initial_provider=True,
                    model=selection.model,
                    thinking_level=thinking_level or "off",
                    storage=InMemorySessionStorage(),
                    cwd=workspace,
                    resource_paths=resource_paths,
                    session_id=f"eval-{call_id}",
                    provider_name=selection.provider.name,
                    provider_settings=self.provider_settings,
                    runtime_provider_config=selection.provider,
                    shell_command_prefix=shell.shell_command_prefix,
                    extension_paths=self.extension_paths,
                    project_extensions_enabled=self.project_extensions_enabled,
                    trust_default=self.trust_default,
                )
            )
            async for event in session.prompt(task.prompt):
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    final = event.message
        finally:
            if session is not None:
                await session.aclose()
            else:
                await provider.aclose()
            ledger.close()

        if final is None:
            raise RuntimeError("coding session produced no assistant response")
        if final.stop_reason in {"error", "aborted"}:
            raise RuntimeError(final.error_message or f"assistant stopped with {final.stop_reason}")
        records = ledger.read_all()
        efficiency = summarize_provider_calls(records)
        estimated_cost = _estimated_ledger_cost(records)
        trace_path = self.paths.traces_dir / f"eval-{call_id}.jsonl"
        if efficiency.total_cost > 0:
            cost: float | None = efficiency.total_cost
            cost_source = "provider_reported"
        elif estimated_cost is not None:
            cost = estimated_cost
            cost_source = "catalog_estimate"
        else:
            cost = None
            cost_source = "unavailable"
        return ExecutionResult(
            output=final.text,
            metadata={
                "calls": efficiency.logical_calls,
                "physical_attempts": efficiency.physical_attempts,
                "retries": efficiency.retry_count,
                "input_tokens": efficiency.input_tokens,
                "output_tokens": efficiency.output_tokens,
                "cache_read_tokens": efficiency.cache_read_tokens,
                "cache_write_tokens": efficiency.cache_write_tokens,
                "cache_write_1h_tokens": efficiency.cache_write_1h_tokens,
                "cost": cost,
                "cost_source": cost_source,
                "provider": selection.provider.name,
                "model": selection.model,
                "call_ledger": str(ledger.path),
                "trace": str(trace_path) if trace_path.is_file() else None,
            },
        )


def _estimated_ledger_cost(records: list[dict[str, Any]]) -> float | None:
    calls = [record for record in records if record.get("type") == "provider_call"]
    estimates = [
        estimated_request_cost(
            str(record.get("provider", "")),
            str(record.get("model", "")),
            fresh=int(record.get("input_tokens", 0)),
            cached=int(record.get("cache_read_tokens", 0)),
            cache_write=int(record.get("cache_write_tokens", 0)),
            cache_write_1h=int(record.get("cache_write_1h_tokens", 0)),
            output=int(record.get("output_tokens", 0)),
        )
        for record in calls
    ]
    if not estimates or any(estimate is None for estimate in estimates):
        return None
    return sum(estimate for estimate in estimates if estimate is not None)


__all__ = ["CodingTaskExecutor"]
