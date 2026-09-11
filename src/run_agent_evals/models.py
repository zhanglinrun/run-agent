"""Frozen evaluation task and trial artifact models."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from run_agent_core.types import JSONValue

PYTHON_PLACEHOLDER = "{python}"

TrialStatus = Literal["passed", "failed", "error", "cancelled"]


@dataclass(frozen=True, slots=True)
class FrozenTask:
    id: str
    fixture: Path
    prompt: str
    verify: tuple[tuple[str, ...], ...]
    tags: tuple[str, ...] = ()
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    output: str = ""
    metadata: dict[str, JSONValue] = field(default_factory=dict)


class ExecutionFailure(RuntimeError):
    """An unsuccessful attempt with its output and accounting still available."""

    def __init__(self, message: str, result: ExecutionResult) -> None:
        super().__init__(message)
        self.result = result


class ExecutionCancelled(asyncio.CancelledError):
    def __init__(self, result: ExecutionResult) -> None:
        super().__init__("execution cancelled")
        self.result = result


@dataclass(frozen=True, slots=True)
class VerifierResult:
    command: tuple[str, ...]
    exit_code: int | None
    duration_ms: float
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class TrialArtifact:
    id: str
    task_id: str
    candidate_id: str
    seed: int
    status: TrialStatus
    prompt: str
    fixture: str
    started_at: float
    duration_ms: float
    workspace_digest_before: str
    workspace_digest_after: str
    executor_output: str
    verifiers: tuple[VerifierResult, ...]
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    error: str | None = None
    artifact_path: Path | None = None

    @classmethod
    def synthetic(
        cls,
        *,
        task_id: str,
        candidate_id: str,
        passed: bool,
        duration_ms: float = 0.0,
    ) -> TrialArtifact:
        return cls(
            id=uuid4().hex,
            task_id=task_id,
            candidate_id=candidate_id,
            seed=0,
            status="passed" if passed else "failed",
            prompt="",
            fixture="",
            started_at=0.0,
            duration_ms=duration_ms,
            workspace_digest_before="",
            workspace_digest_after="",
            executor_output="",
            verifiers=(),
        )

    def with_artifact_path(self, path: Path) -> TrialArtifact:
        return replace(self, artifact_path=path)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_path"] = str(self.artifact_path) if self.artifact_path else None
        return payload

    def write(self, path: Path) -> TrialArtifact:
        stored = self.with_artifact_path(path.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(stored.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return stored

    @classmethod
    def from_path(cls, path: Path) -> TrialArtifact:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            id=str(payload["id"]),
            task_id=str(payload["task_id"]),
            candidate_id=str(payload["candidate_id"]),
            seed=int(payload["seed"]),
            status=payload["status"],
            prompt=str(payload["prompt"]),
            fixture=str(payload["fixture"]),
            started_at=float(payload["started_at"]),
            duration_ms=float(payload["duration_ms"]),
            workspace_digest_before=str(payload["workspace_digest_before"]),
            workspace_digest_after=str(payload["workspace_digest_after"]),
            executor_output=str(payload["executor_output"]),
            verifiers=tuple(
                VerifierResult(
                    command=tuple(item["command"]),
                    exit_code=item["exit_code"],
                    duration_ms=float(item["duration_ms"]),
                    stdout=str(item["stdout"]),
                    stderr=str(item["stderr"]),
                    timed_out=bool(item.get("timed_out", False)),
                )
                for item in payload["verifiers"]
            ),
            metadata=payload.get("metadata", {}),
            error=payload.get("error"),
            artifact_path=Path(payload["artifact_path"]) if payload.get("artifact_path") else None,
        )


__all__ = [
    "ExecutionResult",
    "FrozenTask",
    "PYTHON_PLACEHOLDER",
    "TrialArtifact",
    "TrialStatus",
    "VerifierResult",
]
