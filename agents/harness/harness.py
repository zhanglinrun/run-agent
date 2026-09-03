"""Task-level orchestration around AgentCore and the extension host."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import time
from typing import Any

from ..context.prompt import PromptBuildOptions, build_system_prompt
from ..execution import SandboxError, WorkspaceJournal, git_base_commit, git_changed_paths
from ..extensions import (
    ExtensionContext,
    ExtensionDiscoveryError,
    ExtensionHost,
    ExtensionLoadError,
    ExtensionSpec,
    RunOutcome,
    default_extension_specs,
    discover_extension_specs,
)
from ..runtime.scope import bind_workspace
from ..runtime.tracing import TraceRecorder
from ..session import OperationType, SessionRepository
from .budget import BudgetExceeded, BudgetLedger
from .core_session import CoreSession, build_provider
from .failures import FailureInfo, FailureKind
from .task import ChangeSet, TaskPhase, TaskResult, TaskSpec, TaskState, TaskStatus, Usage


class HarnessConfig:
    def __init__(
        self,
        *,
        session_db: str | Path | None = None,
        extensions: tuple[ExtensionSpec, ...] = (),
    ) -> None:
        self.session_db = (
            Path(session_db).expanduser().resolve() if session_db else None
        )
        self.extensions = tuple(extensions)


class AgentHarness:
    """Own task/session evidence while extensions own optional capabilities."""

    def __init__(self, *, config: HarnessConfig | None = None) -> None:
        self.config = config or HarnessConfig()
        self._active_agent: CoreSession | None = None
        self._running = False

    def abort(self) -> None:
        if self._active_agent is not None:
            self._active_agent.core.abort()

    async def run(self, task: TaskSpec) -> TaskResult:
        if self._running:
            return TaskResult(
                task.task_id,
                TaskStatus.INFRASTRUCTURE_FAILURE,
                failure=FailureInfo(
                    FailureKind.INFRASTRUCTURE,
                    "AgentHarness does not support concurrent run() calls",
                ),
            )
        self._running = True
        started = time.perf_counter()
        try:
            root = Path(task.workspace).expanduser().resolve()
            if not root.is_dir():
                return TaskResult(
                    task.task_id,
                    TaskStatus.INFRASTRUCTURE_FAILURE,
                    failure=FailureInfo(
                        FailureKind.INFRASTRUCTURE,
                        f"workspace is not a directory: {root}",
                    ),
                )
            with bind_workspace(root):
                return await self._run_bound(task, root, started)
        finally:
            self._running = False

    async def _run_bound(
        self, task: TaskSpec, root: Path, started: float
    ) -> TaskResult:
        session_settings = task.runtime.session
        artifact_root = self._artifact_root(task)
        artifact_root.mkdir(parents=True, exist_ok=True)
        db_path = (
            self.config.session_db
            or session_settings.database
            or (artifact_root / "session.db")
        ).expanduser().resolve()
        repository: SessionRepository | None = None
        execution: Any | None = None
        agent: CoreSession | None = None
        host: ExtensionHost | None = None
        context: ExtensionContext | None = None
        state: TaskState | None = None
        trace: TraceRecorder | None = None
        final_text = ""
        final_patch = ""
        report = None
        acceptance = None
        failure: FailureInfo | None = None
        trace_path: Path | None = None
        session_id = ""
        run_id = session_settings.run_id or (
            "run-"
            + hashlib.sha256(
                f"{task.task_id}:{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:12]
        )
        status = TaskStatus.FAILED
        command_invoked = False
        sandbox_lifecycle: dict[str, Any] = {
            "requested": task.runtime.execution.backend == "docker",
            "started": False,
            "closed": False,
            "timed_out": False,
        }
        extension_names: tuple[str, ...] = ()
        try:
            repository = SessionRepository(db_path)
            requested = session_settings.resume_session_id or ""
            session_id = (
                repository.ensure_session(
                    requested,
                    metadata={
                        "task_id": task.task_id,
                        "mode": task.mode,
                        "model": task.runtime.provider.model,
                    },
                )
                if requested
                else repository.create_session(
                    metadata={
                        "task_id": task.task_id,
                        "mode": task.mode,
                        "model": task.runtime.provider.model,
                    }
                )
            )
            lane_id = session_settings.lane_id or "main"
            baseline = git_changed_paths(root)
            journal = WorkspaceJournal(root, baseline_dirty_paths=baseline)
            journal.prime()
            state = TaskState(
                run_id,
                TaskPhase.CREATED,
                session_id,
                lane_id,
                root,
                BudgetLedger(task.budget),
                ChangeSet(
                    base_commit=git_base_commit(root),
                    initial_dirty_paths=tuple(sorted(baseline)),
                ),
            )
            state.journal = journal
            repository.append_operation(
                session_id,
                lane_id,
                OperationType.RUN_STARTED,
                {"task_id": task.task_id, "mode": task.mode},
                run_id=run_id,
            )
            if baseline and task.mode in {"coding", "swebench"}:
                raise SandboxError(
                    "coding and SWE-bench tasks require a clean workspace so task patches cannot include pre-existing user changes"
                )

            specs = self._extension_specs(task)
            extension_names = tuple(spec.name for spec in specs)
            host = ExtensionHost(specs)
            host.load()
            execution = await host.create_execution(task, root)
            state.execution = execution
            sandbox_lifecycle["started"] = task.runtime.execution.backend == "docker"
            provider = build_provider(task)
            trace_root = (
                session_settings.trace_root or artifact_root / "traces"
            ).expanduser().resolve()
            trace = TraceRecorder(
                session_id=session_id,
                model=task.runtime.provider.model,
                root=trace_root,
            )
            trace.start_run(
                task.prompt,
                task_id=task.task_id,
                mode=task.mode,
                run_id=run_id,
            )
            prompt_settings = task.runtime.prompt
            base_prompt = build_system_prompt(
                PromptBuildOptions(
                    workspace=root,
                    custom_prompt=prompt_settings.custom_prompt,
                    append_system_prompt=prompt_settings.append_system_prompt,
                )
            )
            context = ExtensionContext(
                task=task,
                state=state,
                repository=repository,
                journal=journal,
                provider=provider,
                execution=execution,
                artifact_root=artifact_root,
                trace=trace,
                base_prompt=base_prompt,
                outcome=RunOutcome(),
            )
            host.bind(context)
            with host.use_context(context):
                await host.emit("session_start", reason="resume" if requested else "startup")
            agent = CoreSession(
                task=task,
                state=state,
                repository=repository,
                journal=journal,
                provider=provider,
                host=host,
                context=context,
                trace=trace,
            )
            self._active_agent = agent
            context.services.update(
                {
                    "session_middleware": agent.session_middleware,
                    "run_agent": lambda prompt, turns: self._run_agent(
                        agent, prompt, max_turns=turns
                    ),
                    "consume_output": lambda output, phase: self._consume_output(
                        state, output, phase=phase
                    ),
                    "export_patch": lambda: self._export_patch(execution, state),
                }
            )

            command = self._command(task.prompt, host)
            if command is not None:
                command_invoked = True
                name, args = command
                with host.use_context(context):
                    value = await host.dispatch_command(name, args)
                state.budgets.ensure_available()
                final_text = str(value or "")
            else:
                with host.use_context(context):
                    await host.emit("before_run")
                state.phase = TaskPhase.SOLVING
                state.budgets.ensure_turn_available("solve")
                output = await self._run_agent(
                    agent,
                    task.prompt,
                    max_turns=state.budgets.solve_remaining,
                )
                final_text = str(output.get("text") or "")
                self._consume_output(state, output, phase="solve")
                state.phase = TaskPhase.VERIFYING
                journal.observe()
                state.changes.update(journal.changed_paths, journal.content_hashes())
                final_patch = await self._export_patch(execution, state)
                context.outcome.final_text = final_text
                context.outcome.patch = final_patch
                with host.use_context(context):
                    await host.emit("after_solve")
                    await host.emit("after_run")
                final_text = context.outcome.final_text
                final_patch = context.outcome.patch
                report = context.outcome.report
                acceptance = context.outcome.acceptance
                failure = context.outcome.failure
                if task.mode in {"coding", "swebench"} and not final_patch.strip():
                    if failure is None or failure.kind != FailureKind.INFRASTRUCTURE:
                        failure = FailureInfo(
                            FailureKind.EMPTY_PATCH, "the agent produced no patch"
                        )
        except BudgetExceeded as exc:
            if state is not None and execution is not None:
                try:
                    state.journal.observe()
                    state.changes.update(
                        state.journal.changed_paths,
                        state.journal.content_hashes(),
                    )
                    final_patch = await self._export_patch(execution, state)
                except Exception:
                    pass
            failure = FailureInfo(FailureKind.BUDGET, str(exc))
        except asyncio.CancelledError:
            failure = FailureInfo(FailureKind.CANCELLED, "task cancelled")
        except SandboxError as exc:
            failure = FailureInfo(FailureKind.INFRASTRUCTURE, str(exc))
        except (ExtensionDiscoveryError, ExtensionLoadError) as exc:
            failure = FailureInfo(FailureKind.INFRASTRUCTURE, str(exc))
        except Exception as exc:
            kind = (
                FailureKind.MODEL
                if state is not None and state.phase == TaskPhase.SOLVING
                else FailureKind.INFRASTRUCTURE
            )
            failure = FailureInfo(kind, f"{type(exc).__name__}: {exc}")
        finally:
            if state is not None:
                state.phase = TaskPhase.FINALIZING
            if context is not None:
                context.outcome.final_text = final_text
                context.outcome.patch = final_patch
                context.outcome.report = report
                context.outcome.acceptance = acceptance
                context.outcome.failure = failure
            if host is not None and context is not None:
                try:
                    with host.use_context(context):
                        await host.emit("session_shutdown", reason="task_end")
                except Exception as exc:
                    failure = self._infrastructure_cleanup_failure(
                        f"extension cleanup failed: {type(exc).__name__}: {exc}",
                        prior=failure,
                    )
            if execution is not None:
                try:
                    await execution.close()
                except Exception as exc:
                    failure = self._infrastructure_cleanup_failure(
                        f"execution cleanup failed: {type(exc).__name__}: {exc}",
                        prior=failure,
                    )
            lifecycle = (
                getattr(execution, "lifecycle_snapshot", None)
                if execution is not None
                else None
            )
            if callable(lifecycle):
                try:
                    sandbox_lifecycle = lifecycle()
                except Exception as exc:
                    failure = self._infrastructure_cleanup_failure(
                        f"sandbox lifecycle snapshot failed: {type(exc).__name__}: {exc}",
                        prior=failure,
                    )
            verification_enabled = "verification" in extension_names and not command_invoked
            status = self._status(
                task,
                report,
                final_patch,
                failure,
                verification_enabled=verification_enabled,
                command_invoked=command_invoked,
            )
            if repository is not None:
                if session_id:
                    try:
                        repository.append_operation(
                            session_id,
                            state.lane_id if state else "main",
                            OperationType.RUN_FINISHED,
                            {
                                "status": status.value,
                                "failure": failure.to_dict() if failure else None,
                            },
                            run_id=run_id,
                        )
                    except Exception as exc:
                        failure = self._infrastructure_cleanup_failure(
                            f"session finalization failed: {type(exc).__name__}: {exc}",
                            prior=failure,
                        )
                try:
                    repository.close()
                except Exception as exc:
                    failure = self._infrastructure_cleanup_failure(
                        f"session close failed: {type(exc).__name__}: {exc}",
                        prior=failure,
                    )
            if trace is not None:
                trace_path = trace.path
                try:
                    trace.finish_run(
                        answer=final_text,
                        tokens={
                            "input": state.budgets.input_tokens if state else 0,
                            "output": state.budgets.output_tokens if state else 0,
                        },
                        success=failure is None
                        and (report is None or report.outcome == "PASS"),
                        error=failure.message if failure else None,
                    )
                except Exception as exc:
                    failure = self._infrastructure_cleanup_failure(
                        f"trace finalization failed: {type(exc).__name__}: {exc}",
                        prior=failure,
                    )
            status = self._status(
                task,
                report,
                final_patch,
                failure,
                verification_enabled=verification_enabled,
                command_invoked=command_invoked,
            )
            if state is not None:
                state.failure = failure
                state.phase = (
                    TaskPhase.COMPLETED
                    if status == TaskStatus.COMPLETED
                    else TaskPhase.FAILED
                )
            self._active_agent = None

        if state is None:
            return TaskResult(
                task.task_id,
                status,
                answer=final_text,
                patch=final_patch,
                failure=failure,
            )
        return TaskResult(
            task.task_id,
            status,
            answer=final_text,
            patch=final_patch,
            changed_paths=tuple(sorted(state.changes.changed_paths)),
            usage=Usage(
                state.budgets.input_tokens,
                state.budgets.output_tokens,
                state.budgets.cost_usd,
                (time.perf_counter() - started) * 1000,
            ),
            verification=report,
            correction_attempts=tuple(state.correction_history),
            trace_path=Path(trace_path) if trace_path else None,
            session_id=session_id,
            failure=failure,
            metadata={
                **task.metadata,
                "run_id": run_id,
                "budgets": state.budgets.to_dict(),
                "session_db": str(db_path),
                "session_db_sha256": self._file_sha(db_path),
                "extensions": list(extension_names),
                "candidates": [item.__dict__ for item in state.candidates],
                "verification_history": [
                    item.to_dict() for item in state.verification_history
                ],
                "correction_activated": bool(state.correction_history),
                "acceptance": acceptance.to_dict() if acceptance is not None else None,
                "sandbox": sandbox_lifecycle,
                "patch_attribution": "withheld_initial_dirty_workspace"
                if state.changes.initial_dirty_paths
                else "task_workspace_diff",
                "cost_enforcement": "enforced"
                if state.budgets.spec.max_cost_usd is not None
                else "not_configured",
            },
        )

    def _extension_specs(self, task: TaskSpec) -> tuple[ExtensionSpec, ...]:
        settings = task.runtime.extensions
        specs: list[ExtensionSpec] = []
        if settings.use_defaults:
            specs.extend(default_extension_specs(settings.disabled))
        specs.extend(self.config.extensions)
        specs.extend(
            discover_extension_specs(
                task.workspace,
                explicit_paths=settings.explicit_paths,
                load_user=settings.load_user,
                load_project=settings.trust_project,
            )
        )
        return tuple(spec for spec in specs if spec.name not in settings.disabled)

    @staticmethod
    def _command(
        prompt: str, host: ExtensionHost
    ) -> tuple[str, str] | None:
        text = prompt.strip()
        if not text.startswith("/"):
            return None
        name, _, args = text[1:].partition(" ")
        known = {command.name for command in host.commands()}
        return (name, args.strip()) if name in known else None

    @staticmethod
    async def _run_agent(
        agent: Any, prompt: str, *, max_turns: int
    ) -> dict[str, Any]:
        result = agent.run_once(prompt, max_turns=max_turns)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, dict):
            return result
        usage = getattr(result, "usage", None)
        return {
            "text": getattr(result, "answer", str(result)),
            "tokens": usage.to_dict() if hasattr(usage, "to_dict") else {},
            "turns": 0,
        }

    @staticmethod
    def _consume_output(
        state: TaskState, output: dict[str, Any], *, phase: str
    ) -> None:
        if not output.get("turns_accounted"):
            for _ in range(int(output.get("turns", 0) or 0)):
                state.budgets.consume_turn(phase=phase)
        tokens = output.get("tokens") or {}
        if not output.get("usage_accounted"):
            state.budgets.consume_usage(
                input_tokens=int(tokens.get("input", 0) or 0),
                output_tokens=int(tokens.get("output", 0) or 0),
                cost_usd=tokens.get("cost_usd"),
            )

    @staticmethod
    async def _export_patch(execution: Any, state: TaskState) -> str:
        if execution is None or state.changes.initial_dirty_paths:
            return ""
        return await execution.diff()

    @staticmethod
    def _artifact_root(task: TaskSpec) -> Path:
        value = task.runtime.session.artifact_dir
        candidate = (
            Path(value).expanduser().resolve()
            if value
            else Path(tempfile.gettempdir()) / "run-agent" / "tasks" / task.task_id
        )
        workspace = Path(task.workspace).expanduser().resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            return candidate
        workspace_key = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
        return (
            Path(tempfile.gettempdir())
            / "run-agent"
            / "tasks"
            / workspace_key
            / task.task_id
        )

    @staticmethod
    def _infrastructure_cleanup_failure(
        message: str, *, prior: FailureInfo | None
    ) -> FailureInfo:
        details = {"cleanup_error": message}
        if prior is not None:
            details["prior_failure"] = prior.to_dict()
        return FailureInfo(
            FailureKind.INFRASTRUCTURE,
            message,
            retryable=True,
            details=details,
        )

    @staticmethod
    def _status(
        task: TaskSpec,
        report: Any | None,
        patch: str,
        failure: FailureInfo | None,
        *,
        verification_enabled: bool,
        command_invoked: bool,
    ) -> TaskStatus:
        if failure and failure.kind == FailureKind.CANCELLED:
            return TaskStatus.CANCELLED
        if failure and failure.kind == FailureKind.INFRASTRUCTURE:
            return TaskStatus.INFRASTRUCTURE_FAILURE
        if command_invoked:
            return TaskStatus.FAILED if failure else TaskStatus.COMPLETED
        if task.mode in {"coding", "swebench"} and not patch.strip():
            return TaskStatus.EMPTY_PATCH
        if failure is not None or (
            verification_enabled and (report is None or report.outcome != "PASS")
        ):
            return TaskStatus.UNRESOLVED
        return TaskStatus.COMPLETED

    @staticmethod
    def _file_sha(path: Path) -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None


__all__ = ["AgentHarness", "HarnessConfig"]
