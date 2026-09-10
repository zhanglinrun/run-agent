"""Task specification layout and trusted admission (plan 8.3 / 8.4).

``TaskSpec`` here describes one evaluation task. It is deliberately unrelated to
``run_agent_coding.host.contracts.TaskSpec``, which describes a managed host
task; the two never meet.

Admission grades a solution with the task's own grader, in a directory built
from the pristine environment, the artifacts the manifest declares, and the
task's grader assets. Nothing else from the workspace is carried in, so the
agent cannot weaken grading by editing the tests, a conftest, or pytest config
that it can see.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PYTHON_PLACEHOLDER = "{python}"
GRADER_SUBDIR = "grader"
DEFAULT_BUDGET_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One evaluation task as laid out on disk."""

    id: str
    version: str
    instruction: str
    budget_seconds: float
    artifacts: tuple[str, ...]
    environment: Path
    grader: Path
    reference: Path
    grader_command: tuple[str, ...]
    root: Path

    @property
    def resolved_grader_command(self) -> tuple[str, ...]:
        """The grader argv with ``{python}`` replaced by this interpreter."""
        return tuple(
            sys.executable if part == PYTHON_PLACEHOLDER else part for part in self.grader_command
        )


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """Result of grading one solution against a task's authoritative grader."""

    admitted: bool
    reason: str
    output: str
    graded_directory: str | None


def load_task_spec(directory: Path) -> TaskSpec:
    """Read a task directory laid out per the plan's 8.3 section."""
    root = Path(directory)
    document: dict[str, Any] = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
    task: dict[str, Any] = document.get("task") or {}
    grader: dict[str, Any] = document.get("grader") or {}
    artifacts = tuple(str(item) for item in task.get("artifacts") or ())
    command = tuple(str(item) for item in grader.get("command") or ())
    _validate(root, task, artifacts, command)
    return TaskSpec(
        id=str(task["id"]),
        version=str(task.get("version", "0")),
        instruction=(root / "instruction.md").read_text(encoding="utf-8").strip(),
        budget_seconds=float(str(task.get("budget_seconds", DEFAULT_BUDGET_SECONDS))),
        artifacts=artifacts,
        environment=root / "environment",
        grader=root / GRADER_SUBDIR,
        reference=root / "reference",
        grader_command=command,
        root=root,
    )


def _validate(
    root: Path,
    task: dict[str, Any],
    artifacts: tuple[str, ...],
    command: tuple[str, ...],
) -> None:
    """Refuse a task directory that cannot be graded unambiguously."""
    if not task.get("id"):
        raise ValueError(f"{root}: task.toml needs task.id")
    if not artifacts:
        raise ValueError(f"{root}: task.toml needs a non-empty task.artifacts")
    if not command:
        raise ValueError(f"{root}: task.toml needs a non-empty grader.command")
    for name in ("environment", GRADER_SUBDIR):
        if not (root / name).is_dir():
            raise ValueError(f"{root}: {name}/ directory is required")


def materialize_environment(spec: TaskSpec, destination: Path) -> None:
    """Copy only ``environment/`` into the agent workspace."""
    target = Path(destination)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(spec.environment, target)


def _seed_grading_directory(spec: TaskSpec, workspace: Path, destination: Path) -> None:
    """Build the grading tree from pristine environment, declared artifacts and grader."""
    shutil.copytree(spec.environment, destination)
    for artifact in spec.artifacts:
        source = workspace / artifact
        if not source.is_file():
            raise FileNotFoundError(f"declared artifact is missing: {artifact}")
        target = destination / artifact
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(spec.grader, destination / GRADER_SUBDIR)


def _grader_environment(directory: Path) -> dict[str, str]:
    """Make the grading root importable without relying on the workspace."""
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(directory), *(item for item in existing.split(os.pathsep) if item)]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(parts)}


def _run_grader(spec: TaskSpec, directory: Path) -> subprocess.CompletedProcess[str]:
    """Run the task's grader with the grading root on PYTHONPATH."""
    return subprocess.run(
        spec.resolved_grader_command,
        cwd=directory,
        env=_grader_environment(directory),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=spec.budget_seconds,
        check=False,
    )


def admit_task(spec: TaskSpec, workspace: Path) -> AdmissionOutcome:
    """Grade a solution outside the workspace and report the outcome."""
    container = Path(tempfile.mkdtemp(prefix=f"admit-{spec.id}-"))
    destination = container / "grading"
    try:
        _seed_grading_directory(spec, Path(workspace), destination)
    except (OSError, FileNotFoundError) as problem:
        return AdmissionOutcome(False, f"cannot prepare grading: {problem}", "", str(destination))
    try:
        completed = _run_grader(spec, destination)
    except subprocess.TimeoutExpired:
        return AdmissionOutcome(False, "grader exceeded the task budget", "", str(destination))
    output = f"{completed.stdout}{completed.stderr}"
    passed = completed.returncode == 0
    reason = "grader passed" if passed else f"grader exited {completed.returncode}"
    return AdmissionOutcome(passed, reason, output, str(destination))
