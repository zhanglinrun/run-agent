"""Stable data contracts for execution backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..runtime.scope import current_workspace


@dataclass(frozen=True)
class SandboxSpec:
    image: str = "run-agent-python-sandbox:latest"
    workspace: Path = field(default_factory=current_workspace)
    network: str = "none"
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 256
    timeout_seconds: float = 180.0
    patch_timeout_seconds: float = 600.0
    container_workspace: str = "/workspace"
    python_executable: str = "python"
    verification_profile: str = "default"
    run_id: str = ""
    case_id: str = ""

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).expanduser().resolve()
        object.__setattr__(self, "workspace", workspace)
        if not self.image.strip():
            raise ValueError("sandbox image must not be empty")
        if self.network not in {"none", "bridge", "host"} and not self.network.startswith("container:"):
            raise ValueError(f"unsupported sandbox network: {self.network}")
        if self.container_workspace not in {"/workspace", "/testbed"}:
            raise ValueError("container_workspace must be /workspace or /testbed")
        if not self.python_executable.strip():
            raise ValueError("sandbox python executable must not be empty")
        if not self.verification_profile.strip():
            raise ValueError("sandbox verification profile must not be empty")
        if (
            self.memory_mb <= 0
            or self.cpus <= 0
            or self.pids_limit <= 0
            or self.timeout_seconds <= 0
            or self.patch_timeout_seconds <= 0
        ):
            raise ValueError("sandbox resource limits must be positive")


@dataclass(frozen=True)
class ExecRequest:
    argv: tuple[str, ...]
    cwd: str = "/workspace"
    timeout_seconds: float = 120.0
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        argv = tuple(str(item) for item in self.argv)
        object.__setattr__(self, "argv", argv)
        if not argv or any(not item for item in argv):
            raise ValueError("sandbox exec argv must be a non-empty sequence")
        if not self.cwd.startswith("/"):
            raise ValueError("sandbox cwd must be an absolute container path")
        if self.timeout_seconds <= 0:
            raise ValueError("exec timeout must be positive")
        object.__setattr__(self, "env", {str(k): str(v) for k, v in self.env.items()})


@dataclass(frozen=True)
class ExecResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
        }


__all__ = ["ExecRequest", "ExecResult", "SandboxSpec"]
