"""Isolated trial execution and offline artifact reduction."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import Protocol
from uuid import uuid4

from run_agent_evals.models import (
    ExecutionResult,
    FrozenTask,
    TrialArtifact,
    VerifierResult,
)

_MAX_CAPTURE_CHARS = 50_000
_IGNORED_WORKSPACE_NAMES = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
_IGNORED_WORKSPACE_GLOBS = (*_IGNORED_WORKSPACE_NAMES, "*.pyc", "*.pyo")


class TaskExecutor(Protocol):
    async def execute(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        """Run one candidate against an isolated task workspace."""
        ...


class EvaluationRunner:
    def __init__(self, artifact_dir: str | Path, *, keep_workspaces: bool = False) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.keep_workspaces = keep_workspaces

    async def run_trial(
        self,
        task: FrozenTask,
        executor: TaskExecutor,
        *,
        candidate_id: str,
        seed: int,
    ) -> TrialArtifact:
        trial_id = uuid4().hex
        workspace = self.artifact_dir / "workspaces" / trial_id
        artifact_path = self.artifact_dir / "trials" / f"{trial_id}.json"
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            task.fixture,
            workspace,
            ignore=shutil.ignore_patterns(*_IGNORED_WORKSPACE_GLOBS),
        )
        before = workspace_digest(workspace)
        started_at = time()
        started_monotonic = monotonic()
        execution = ExecutionResult()
        verifiers: list[VerifierResult] = []
        error: str | None = None
        status = "error"
        try:
            execution = await executor.execute(task, workspace)
            for command in task.verify:
                result = await run_verifier(
                    command,
                    cwd=workspace,
                    timeout_seconds=task.timeout_seconds,
                )
                verifiers.append(result)
            status = (
                "passed"
                if verifiers
                and all(result.exit_code == 0 and not result.timed_out for result in verifiers)
                else "failed"
            )
        except asyncio.CancelledError:
            status = "cancelled"
            error = "trial cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - executor is an evaluation boundary
            status = "error"
            error = str(exc) or type(exc).__name__
        finally:
            after = workspace_digest(workspace)
            artifact = TrialArtifact(
                id=trial_id,
                task_id=task.id,
                candidate_id=candidate_id,
                seed=seed,
                status=status,  # type: ignore[arg-type]
                prompt=task.prompt,
                fixture=str(task.fixture),
                started_at=started_at,
                duration_ms=round((monotonic() - started_monotonic) * 1000, 3),
                workspace_digest_before=before,
                workspace_digest_after=after,
                executor_output=execution.output,
                verifiers=tuple(verifiers),
                metadata=execution.metadata,
                error=error,
            ).write(artifact_path)
            if not self.keep_workspaces:
                shutil.rmtree(workspace, ignore_errors=True)
        return artifact


async def run_verifier(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> VerifierResult:
    started = monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout, stderr = await process.communicate()
    return VerifierResult(
        command=tuple(command),
        exit_code=process.returncode,
        duration_ms=round((monotonic() - started) * 1000, 3),
        stdout=_bounded_decode(stdout),
        stderr=_bounded_decode(stderr),
        timed_out=timed_out,
    )


def _bounded_decode(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= _MAX_CAPTURE_CHARS:
        return text
    return text[-_MAX_CAPTURE_CHARS:]


def workspace_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file()
        and not _IGNORED_WORKSPACE_NAMES.intersection(candidate.relative_to(root).parts)
        and candidate.suffix not in {".pyc", ".pyo"}
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    candidate_id: str
    trials: int
    passed: int
    failed: int
    errored: int
    pass_rate: float
    p50_duration_ms: float
    p95_duration_ms: float
    total_calls: int
    priced_trials: int
    total_cost: float | None
    task_ids: tuple[str, ...]

    @property
    def cost_per_passed_trial(self) -> float | None:
        if self.total_cost is None or not self.passed:
            return None
        return self.total_cost / self.passed

    @property
    def calls_per_passed_trial(self) -> float | None:
        return self.total_calls / self.passed if self.passed else None


def reduce_trials(trials: Sequence[TrialArtifact]) -> EvaluationSummary:
    if not trials:
        return EvaluationSummary("", 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0, None, ())
    candidates = {trial.candidate_id for trial in trials}
    if len(candidates) != 1:
        raise ValueError("a reduction must contain exactly one candidate_id")
    durations = sorted(trial.duration_ms for trial in trials)
    passed = sum(trial.status == "passed" for trial in trials)
    failed = sum(trial.status == "failed" for trial in trials)
    errored = len(trials) - passed - failed
    costs = [_metadata_optional_float(trial, "cost") for trial in trials]
    priced_costs = [cost for cost in costs if cost is not None]
    return EvaluationSummary(
        candidate_id=next(iter(candidates)),
        trials=len(trials),
        passed=passed,
        failed=failed,
        errored=errored,
        pass_rate=passed / len(trials),
        p50_duration_ms=_percentile(durations, 0.50),
        p95_duration_ms=_percentile(durations, 0.95),
        total_calls=sum(_metadata_int(trial, "calls") for trial in trials),
        priced_trials=len(priced_costs),
        total_cost=sum(priced_costs) if len(priced_costs) == len(trials) else None,
        task_ids=tuple(sorted({trial.task_id for trial in trials})),
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = round((len(values) - 1) * quantile)
    return values[max(0, min(index, len(values) - 1))]


def _metadata_int(trial: TrialArtifact, key: str) -> int:
    value = trial.metadata.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


def _metadata_optional_float(trial: TrialArtifact, key: str) -> float | None:
    value = trial.metadata.get(key)
    return float(value) if isinstance(value, int | float) else None


__all__ = [
    "EvaluationRunner",
    "EvaluationSummary",
    "TaskExecutor",
    "reduce_trials",
    "run_verifier",
    "workspace_digest",
]
