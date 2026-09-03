"""Thin public facade over the typed Harness and default extension profile."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import time
from typing import Any, Awaitable, Callable, Literal, Sequence

from ..execution import SandboxBackend, SandboxSession, SandboxSpec
from ..extensions import default_extension_specs
from ..harness import (
    AgentHarness,
    BudgetSpec,
    ExecutionSettings,
    ExtensionSettings,
    HarnessConfig,
    PermissionSettings,
    PromptSettings,
    ProviderSettings,
    RuntimeConfig,
    SessionSettings,
    TaskResult,
    TaskSpec,
    VerificationSettings,
)
from ..runtime.scope import bind_workspace
from ..runtime.ui import print_assistant_text, print_cost, print_info
from ..session import SessionRepository
from ..verification.discovery import VerificationCommand


ConfirmFn = Callable[[str], Awaitable[bool]]
PlanApprovalFn = Callable[[str], Awaitable[dict[str, Any] | bool]]


class Agent:
    """Own user-facing configuration and delegate every capability to extensions."""

    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "deepseek-chat",
        api_base: str | None = None,
        api_key: str | None = None,
        thinking: bool = False,
        max_cost_usd: float | None = None,
        input_cost_per_million: float = 3.0,
        output_cost_per_million: float = 15.0,
        max_turns: int | None = None,
        confirm_fn: ConfirmFn | None = None,
        custom_system_prompt: str | None = None,
        append_system_prompt: str = "",
        use_openai: bool | None = None,
        trace_root: str | Path | None = None,
        trace_metadata: dict[str, Any] | None = None,
        persist_session: bool = True,
        max_repair_attempts: int = 2,
        require_patch: bool = False,
        temperature: float | None = None,
        execution_backend: Literal["local", "docker"] = "local",
        sandbox_spec: SandboxSpec | None = None,
        sandbox_backend: SandboxBackend | None = None,
        sandbox_session: SandboxSession | None = None,
        allow_host_shell: bool = False,
        verification_commands: Sequence[VerificationCommand] | None = None,
        acceptance_commands: Sequence[VerificationCommand] | None = None,
        workspace: str | Path | None = None,
        session_db: str | Path | None = None,
        extension_paths: Sequence[str | Path] = (),
        disable_extensions: Sequence[str] = (),
        use_default_extensions: bool = True,
        load_user_extensions: bool = True,
        trust_project_extensions: bool = False,
    ) -> None:
        if execution_backend not in {"local", "docker"}:
            raise ValueError("execution_backend must be 'local' or 'docker'")
        if input_cost_per_million < 0 or output_cost_per_million < 0:
            raise ValueError("cost rates must be non-negative")
        self.permission_mode = permission_mode
        self.model = model
        self.api_base = api_base
        self.api_key = api_key or ""
        self.thinking = bool(thinking)
        self.temperature = temperature
        self.max_cost_usd = max_cost_usd
        self.max_turns = max(1, int(max_turns or 18))
        self.max_repair_attempts = max(0, int(max_repair_attempts))
        self.input_cost_per_million = float(input_cost_per_million)
        self.output_cost_per_million = float(output_cost_per_million)
        self.use_openai = bool(use_openai) if use_openai is not None else bool(api_base)
        self.execution_backend = execution_backend
        self.sandbox_spec = sandbox_spec
        self.sandbox_backend = sandbox_backend
        self.sandbox_session = sandbox_session
        self.allow_host_shell = bool(allow_host_shell)
        self.verification_commands = tuple(verification_commands or ()) or None
        self.acceptance_commands = tuple(acceptance_commands or ()) or None
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self.trace_root = Path(trace_root).expanduser().resolve() if trace_root else None
        self.trace_metadata = dict(trace_metadata or {})
        self.custom_system_prompt = custom_system_prompt
        self.append_system_prompt = append_system_prompt
        self.require_patch = bool(require_patch)
        self.extension_paths = tuple(Path(path) for path in extension_paths)
        self.disable_extensions = set(disable_extensions)
        self.use_default_extensions = bool(use_default_extensions)
        self.load_user_extensions = bool(load_user_extensions)
        self.trust_project_extensions = bool(trust_project_extensions)
        self._confirm_fn = confirm_fn
        self._plan_approval_fn: PlanApprovalFn | None = None
        self._current_task: asyncio.Task[TaskResult] | None = None
        self._aborted = False
        self._last_result: TaskResult | None = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.session_id = ""

        self._temporary_state: tempfile.TemporaryDirectory[str] | None = None
        if session_db is not None:
            resolved_db = Path(session_db).expanduser().resolve()
        elif persist_session:
            resolved_db = self.workspace / ".run" / "sessions.db"
        else:
            self._temporary_state = tempfile.TemporaryDirectory(
                prefix="run-agent-session-"
            )
            resolved_db = Path(self._temporary_state.name) / "sessions.db"
        self.session_db = resolved_db
        self.harness = AgentHarness(config=HarnessConfig(session_db=resolved_db))

    @property
    def last_result(self) -> TaskResult | None:
        return self._last_result

    @property
    def trace_path(self) -> Path | None:
        return self._last_result.trace_path if self._last_result else None

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def is_running(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def set_confirm_fn(self, callback: ConfirmFn | None) -> None:
        self._confirm_fn = callback

    def set_plan_approval_fn(self, callback: PlanApprovalFn | None) -> None:
        self._plan_approval_fn = callback

    def abort(self) -> None:
        self._aborted = True
        self.harness.abort()
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()

    def resume(self, session_id: str) -> bool:
        try:
            with SessionRepository(self.session_db) as repository:
                exists = repository.get_session(session_id) is not None
        except Exception:
            return False
        if exists:
            self.session_id = session_id
        return exists

    def list_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with SessionRepository(self.session_db) as repository:
            return repository.list_sessions(limit=limit)

    def clear_history(self) -> None:
        self.session_id = ""
        print_info("Started a new SQLite session.")

    def show_cost(self) -> None:
        print_cost(
            self.total_input_tokens,
            self.total_output_tokens,
            input_cost_per_million=self.input_cost_per_million,
            output_cost_per_million=self.output_cost_per_million,
        )

    def _budget(self) -> BudgetSpec:
        disabled = self._disabled_extensions()
        default_names = (
            {spec.name for spec in default_extension_specs(disabled)}
            if self.use_default_extensions
            else set()
        )
        correction_enabled = "correction" in default_names
        if correction_enabled:
            repair = min(4, max(0, self.max_turns - 1), self.max_repair_attempts * 2)
        else:
            repair = 0
        return BudgetSpec(
            total_turns=self.max_turns,
            solve_turns=self.max_turns - repair,
            repair_turns=repair,
            max_repair_attempts=self.max_repair_attempts if repair else 0,
            max_cost_usd=self.max_cost_usd,
            input_cost_per_million=self.input_cost_per_million,
            output_cost_per_million=self.output_cost_per_million,
        )

    def _disabled_extensions(self) -> frozenset[str]:
        disabled = set(self.disable_extensions)
        if self.execution_backend != "local":
            disabled.add("mcp")
        return frozenset(disabled)

    def _task(self, prompt: str) -> TaskSpec:
        task_id = str(
            self.trace_metadata.get("case_id") or f"interactive-{time.time_ns()}"
        )
        runtime = RuntimeConfig(
            provider=ProviderSettings(
                model=self.model,
                api_key=self.api_key,
                api_base=self.api_base,
                use_openai=self.use_openai,
                temperature=self.temperature,
                thinking=self.thinking,
            ),
            permissions=PermissionSettings(
                mode=self.permission_mode,
                confirm=self._confirm_fn,
                plan_approval=self._plan_approval_fn,
            ),
            execution=ExecutionSettings(
                backend=self.execution_backend,
                sandbox_spec=self.sandbox_spec,
                sandbox_backend=self.sandbox_backend,
                sandbox_session=self.sandbox_session,
                allow_host_shell=self.allow_host_shell,
            ),
            session=SessionSettings(
                database=self.session_db,
                resume_session_id=self.session_id or None,
                trace_root=self.trace_root,
            ),
            prompt=PromptSettings(
                custom_prompt=self.custom_system_prompt,
                append_system_prompt=self.append_system_prompt,
            ),
            verification=VerificationSettings(
                commands=self.verification_commands,
                acceptance_commands=self.acceptance_commands,
            ),
            extensions=ExtensionSettings(
                use_defaults=self.use_default_extensions,
                disabled=self._disabled_extensions(),
                explicit_paths=self.extension_paths,
                load_user=self.load_user_extensions,
                trust_project=self.trust_project_extensions,
            ),
        )
        return TaskSpec(
            task_id=task_id,
            prompt=prompt,
            workspace=self.workspace,
            mode="coding" if self.require_patch else "interactive",
            verification_profile="default",
            budget=self._budget(),
            runtime=runtime,
            metadata=self.trace_metadata,
        )

    async def run_once(self, prompt: str) -> TaskResult:
        self._aborted = False
        self._current_task = asyncio.create_task(self.harness.run(self._task(prompt)))
        try:
            with bind_workspace(self.workspace):
                result = await self._current_task
        except asyncio.CancelledError:
            self._aborted = True
            raise
        finally:
            self._current_task = None
        self._last_result = result
        if result.session_id:
            self.session_id = result.session_id
        self.total_input_tokens += result.usage.input_tokens
        self.total_output_tokens += result.usage.output_tokens
        return result

    async def chat(self, user_message: str) -> TaskResult:
        result = await self.run_once(user_message)
        if result.answer:
            print_assistant_text(result.answer)
        return result

    async def close(self) -> None:
        if self._temporary_state is not None:
            self._temporary_state.cleanup()
            self._temporary_state = None


__all__ = ["Agent"]
