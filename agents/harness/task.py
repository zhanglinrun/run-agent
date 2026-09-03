"""Typed task, runtime configuration, state, and result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from ..verification.models import VerificationReport
from .budget import BudgetLedger, BudgetSpec
from .failures import FailureInfo


ConfirmFn = Callable[[str], bool | Awaitable[bool]]
PlanApprovalResult = bool | dict[str, Any]
PlanApprovalFn = Callable[
    [str], PlanApprovalResult | Awaitable[PlanApprovalResult]
]


class TaskStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    CANCELLED = "cancelled"
    EMPTY_PATCH = "empty_patch"


class TaskPhase(str, Enum):
    CREATED = "created"
    SOLVING = "solving"
    VERIFYING = "verifying"
    CORRECTING = "correcting"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderSettings:
    adapter: Any | None = None
    model: str = "deepseek-chat"
    api_key: str = ""
    api_base: str | None = None
    use_openai: bool = False
    temperature: float | None = None
    thinking: bool = False


@dataclass(frozen=True)
class PermissionSettings:
    mode: Literal[
        "default", "acceptEdits", "dontAsk", "bypassPermissions", "plan"
    ] = "default"
    confirm: ConfirmFn | None = None
    plan_approval: PlanApprovalFn | None = None
    plan_file: Path | None = None


@dataclass(frozen=True)
class ExecutionSettings:
    backend: Literal["local", "docker"] = "local"
    environment: Any | None = None
    sandbox_spec: Any | None = None
    sandbox_backend: Any | None = None
    sandbox_session: Any | None = None
    sandbox_image: str = "run-agent-python-sandbox:latest"
    network: str = "none"
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 256
    timeout_seconds: float = 180.0
    patch_timeout_seconds: float = 600.0
    container_workspace: str = "/workspace"
    python_executable: str = "python"
    allow_host_shell: bool = False


@dataclass(frozen=True)
class SessionSettings:
    database: Path | None = None
    resume_session_id: str | None = None
    lane_id: str = "main"
    resume_messages: tuple[dict[str, Any], ...] | None = None
    artifact_dir: Path | None = None
    trace_root: Path | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class PromptSettings:
    custom_prompt: str | None = None
    append_system_prompt: str = ""
    context_message_limit: int = 48
    keep_recent_messages: int = 12


@dataclass(frozen=True)
class VerificationSettings:
    commands: tuple[Any, ...] | None = None
    acceptance_commands: tuple[Any, ...] | None = None
    timeout_seconds: float = 180.0
    disposable_workspace: bool = False


@dataclass(frozen=True)
class ExtensionSettings:
    """Select the default profile and trusted external Python extensions."""

    use_defaults: bool = True
    disabled: frozenset[str] = frozenset()
    explicit_paths: tuple[Path, ...] = ()
    load_user: bool = True
    trust_project: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    provider: ProviderSettings = field(default_factory=ProviderSettings)
    permissions: PermissionSettings = field(default_factory=PermissionSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    prompt: PromptSettings = field(default_factory=PromptSettings)
    verification: VerificationSettings = field(default_factory=VerificationSettings)
    extensions: ExtensionSettings = field(default_factory=ExtensionSettings)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ChangeSet:
    base_commit: str = ""
    initial_dirty_paths: tuple[str, ...] = ()
    changed_paths: set[str] = field(default_factory=set)
    hashes: dict[str, str | None] = field(default_factory=dict)

    def update(
        self,
        paths: tuple[str, ...] | list[str] | set[str],
        hashes: dict[str, str | None] | None = None,
    ) -> None:
        self.changed_paths = {str(path) for path in paths}
        self.hashes = (
            {str(path): value for path, value in hashes.items()}
            if hashes
            else {}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "initial_dirty_paths": list(self.initial_dirty_paths),
            "changed_paths": sorted(self.changed_paths),
            "hashes": dict(sorted(self.hashes.items())),
        }


@dataclass(frozen=True)
class PatchCandidate:
    candidate_id: str
    patch_sha256: str
    changed_paths: tuple[str, ...]
    outcome: str
    passed_steps: int = 0
    failed_steps: int = 0
    created_at_turn: int = 0
    patch_path: str | None = None
    required_gates_passed: bool = False
    patch_bytes: int = 0


@dataclass(frozen=True)
class CorrectionAttempt:
    attempt: int
    action: str
    reason: str
    before_patch_sha256: str | None = None
    after_patch_sha256: str | None = None
    verification_fingerprint: str | None = None


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    prompt: str
    workspace: Path
    mode: Literal["interactive", "coding", "swebench"] = "coding"
    verification_profile: str = "default"
    budget: BudgetSpec = field(default_factory=BudgetSpec)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskState:
    run_id: str
    phase: TaskPhase
    session_id: str
    lane_id: str
    workspace: Path
    budgets: BudgetLedger
    changes: ChangeSet = field(default_factory=ChangeSet)
    candidates: list[PatchCandidate] = field(default_factory=list)
    candidate_reports: dict[str, VerificationReport] = field(default_factory=dict)
    verification_history: list[VerificationReport] = field(default_factory=list)
    correction_history: list[CorrectionAttempt] = field(default_factory=list)
    failure: FailureInfo | None = None
    execution: Any | None = None
    journal: Any | None = None


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    answer: str = ""
    patch: str = ""
    changed_paths: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)
    verification: VerificationReport | None = None
    correction_attempts: tuple[CorrectionAttempt, ...] = ()
    trace_path: Path | None = None
    session_id: str = ""
    failure: FailureInfo | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status == TaskStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "resolved": self.resolved,
            "answer": self.answer,
            "patch": self.patch,
            "changed_paths": list(self.changed_paths),
            "usage": self.usage.to_dict(),
            "verification": self.verification.to_dict() if self.verification else None,
            "correction_attempts": [item.__dict__ for item in self.correction_attempts],
            "trace_path": str(self.trace_path) if self.trace_path else None,
            "session_id": self.session_id,
            "failure": self.failure.to_dict() if self.failure else None,
            "metadata": self.metadata,
        }


__all__ = [
    "ChangeSet",
    "CorrectionAttempt",
    "ExecutionSettings",
    "ExtensionSettings",
    "PatchCandidate",
    "PermissionSettings",
    "PromptSettings",
    "ProviderSettings",
    "RuntimeConfig",
    "SessionSettings",
    "TaskPhase",
    "TaskResult",
    "TaskSpec",
    "TaskState",
    "TaskStatus",
    "Usage",
    "VerificationSettings",
]
