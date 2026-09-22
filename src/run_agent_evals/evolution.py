"""Paired, evidence-backed evaluation for verifier-gated Skill candidates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from run_agent_coding.host.evaluation import EvaluationReport, EvaluationRequest
from run_agent_core.types import JSONValue
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.grader_runner import GraderSuiteRunner
from run_agent_evals.models import ExecutionCancelled, ExecutionFailure, FrozenTask
from run_agent_evals.task_spec import TaskSpec, load_task_spec, materialize_environment
from run_agent_evals.verifier import DualPropositionVerifier
from run_agent_extensions.experience.candidates import SkillCandidate, SkillCandidateStore
from run_agent_extensions.experience.skill_manager import SkillManager

EVOLUTION_REPORT_SCHEMA = "run-agent.evolution-report.v1"
Split = Literal["train", "selection", "test"]
Arm = Literal["baseline", "candidate"]


@dataclass(frozen=True, slots=True)
class EvolutionSuiteTask:
    id: str
    split: Split
    topic: str
    path: Path


@dataclass(frozen=True, slots=True)
class EvolutionSuite:
    path: Path
    version: str
    family: str
    tasks: tuple[EvolutionSuiteTask, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class EvolutionTrial:
    task_id: str
    split: Split
    arm: Arm
    repeat: int
    succeeded: bool
    infrastructure_error: str | None
    duration_ms: float
    metadata: Mapping[str, JSONValue]
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    unmet_targets: tuple[str, ...] = ()
    newly_failing: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()


class TaskExecutor(Protocol):
    async def execute(
        self, spec: TaskSpec, workspace: Path, state_root: Path
    ) -> Mapping[str, JSONValue]: ...


class CodingExecutor:
    """Run the production CodingApplication with one isolated Experience root."""

    def __init__(
        self,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level_override: str | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.thinking_level_override = thinking_level_override

    async def execute(
        self, spec: TaskSpec, workspace: Path, state_root: Path
    ) -> Mapping[str, JSONValue]:
        executor = CodingTaskExecutor(
            state_root,
            provider_name=self.provider_name,
            model=self.model,
            thinking_level_override=cast(Any, self.thinking_level_override),
            trust_default="never",
        )
        task = FrozenTask(
            id=spec.id,
            fixture=spec.environment,
            prompt=spec.instruction,
            verify=(),
            tags=("evolution",),
            timeout_seconds=spec.budget_seconds,
        )
        result = await executor.execute(task, workspace)
        return result.metadata


class EvolutionEvaluationService:
    """Host-owned paired evaluator with immutable evidence and offline rebuild."""

    available = True

    def __init__(
        self,
        *,
        suite: Path,
        output_root: Path,
        candidates: SkillCandidateStore,
        skills: SkillManager,
        executor: TaskExecutor | None = None,
        repeats: int = 3,
    ) -> None:
        if repeats < 1:
            raise ValueError("Evolution repeats must be positive")
        self.suite = load_evolution_suite(suite)
        self.output_root = output_root.resolve()
        self.candidates = candidates
        self.skills = skills
        self.executor = executor or CodingExecutor()
        self.repeats = repeats

    async def submit(self, request: EvaluationRequest) -> str:
        candidate = self.candidates.require(request.candidate_id)
        self._validate_request(request, candidate)
        existing = self._existing_report(request)
        if existing is not None:
            return existing
        report_id = uuid4().hex
        root = self.output_root / report_id
        if root.exists():
            raise FileExistsError(root)
        root.mkdir(parents=True)
        report = await self._evaluate(report_id, request, candidate, root)
        _write_json(root / "report.json", report)
        inventory = _inventory(root, exclude={"inventory.json"})
        _write_json(
            root / "inventory.json", {"schema": EVOLUTION_REPORT_SCHEMA, "files": inventory}
        )
        return report_id

    async def report(self, report_id: str) -> EvaluationReport:
        root = self.output_root / report_id
        document = rebuild_evolution_report(root)
        request = EvaluationRequest(**document["request"])
        return EvaluationReport(
            report_id=report_id,
            request=request,
            measured_content_hash=str(document["measured_content_hash"]),
            passed=bool(document["passed"]),
            summary=cast(Mapping[str, JSONValue], document["summary"]),
        )

    async def _evaluate(
        self,
        report_id: str,
        request: EvaluationRequest,
        candidate: SkillCandidate,
        root: Path,
    ) -> dict[str, Any]:
        baseline_content = self.skills.main_content(candidate.scope, candidate.name)
        candidate_content = self.candidates.content(candidate)
        if _digest_optional(baseline_content) != candidate.base_digest:
            raise ValueError("formal Skill no longer matches the candidate baseline")
        if _sha256(candidate_content.encode()) != request.content_hash:
            raise ValueError("candidate content hash changed before evaluation")

        trials: list[EvolutionTrial] = []
        for arm, content in (("baseline", baseline_content), ("candidate", candidate_content)):
            state_root = root / "state" / arm
            _install_skill(state_root, candidate.name, content)
            measured = _installed_digest(state_root, candidate.name)
            expected = candidate.base_digest if arm == "baseline" else candidate.candidate_digest
            if measured != expected:
                raise ValueError(f"{arm} Skill digest mismatch before execution")
            for task in self.suite.tasks:
                if task.split == "train":
                    continue
                spec = load_task_spec(task.path)
                for repeat in range(self.repeats):
                    trials.append(
                        await self._run_trial(
                            cast(Arm, arm),
                            task,
                            spec,
                            repeat,
                            state_root,
                            root,
                            skill_name=candidate.name,
                            skill_digest=expected,
                            invoke_skill=content is not None,
                            budget_seconds=min(spec.budget_seconds, request.budget_seconds),
                        )
                    )

        trial_rows = [_trial_json(trial) for trial in trials]
        _write_json(root / "trials.json", trial_rows)
        summary = reduce_evolution_trials(trials, repeats=self.repeats)
        passed = bool(summary["selection_gate"]["passed"])
        source = {
            "suite": str(self.suite.path),
            "suite_digest": self.suite.digest,
            "family": self.suite.family,
            "version": self.suite.version,
            "repeats": self.repeats,
            "candidate_id": candidate.candidate_id,
            "candidate_digest": candidate.candidate_digest,
            "baseline_digest": candidate.base_digest,
            "skill": candidate.name,
            "scope": candidate.scope,
            "task_digests": {task.id: _directory_digest(task.path) for task in self.suite.tasks},
        }
        _write_json(root / "source.json", source)
        return {
            "schema": EVOLUTION_REPORT_SCHEMA,
            "report_id": report_id,
            "request": asdict(request),
            "measured_content_hash": candidate.candidate_digest,
            "passed": passed,
            "summary": summary,
            "source": source,
        }

    async def _run_trial(
        self,
        arm: Arm,
        task: EvolutionSuiteTask,
        spec: TaskSpec,
        repeat: int,
        state_root: Path,
        report_root: Path,
        *,
        skill_name: str,
        skill_digest: str | None,
        invoke_skill: bool,
        budget_seconds: float,
    ) -> EvolutionTrial:
        trial_root = report_root / "workspaces" / arm / task.id / str(repeat)
        workspace = trial_root / "candidate"
        pristine = trial_root / "pristine"
        materialize_environment(spec, workspace)
        materialize_environment(spec, pristine)
        execution_spec = replace(
            spec,
            instruction=(
                f"/skill:{skill_name} {spec.instruction}" if invoke_skill else spec.instruction
            ),
        )
        started = time.perf_counter()
        metadata: Mapping[str, JSONValue] = {}
        error: str | None = None
        try:
            measured = await asyncio.wait_for(
                self.executor.execute(execution_spec, workspace, state_root),
                timeout=budget_seconds,
            )
            metadata = {**measured, "evaluated_skill_digest": skill_digest}
        except (ExecutionFailure, ExecutionCancelled) as exc:
            result = getattr(exc, "result", None)
            metadata = result.metadata if result is not None else {}
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        metadata = {**metadata, "evaluated_skill_digest": skill_digest}

        duration_ms = (time.perf_counter() - started) * 1000
        if error is not None:
            return EvolutionTrial(
                task.id, task.split, arm, repeat, False, error, duration_ms, metadata
            )
        try:
            proposition = await DualPropositionVerifier(GraderSuiteRunner(spec), repeats=1).verify(
                pristine, workspace, targets=spec.fail_to_pass
            )
        except Exception as exc:
            return EvolutionTrial(
                task.id,
                task.split,
                arm,
                repeat,
                False,
                f"grader: {type(exc).__name__}: {exc}",
                duration_ms,
                metadata,
            )
        return EvolutionTrial(
            task.id,
            task.split,
            arm,
            repeat,
            proposition.succeeded,
            None,
            duration_ms,
            metadata,
            tuple(sorted(proposition.fail_to_pass)),
            tuple(sorted(proposition.pass_to_pass)),
            tuple(sorted(proposition.unmet_targets)),
            tuple(sorted(proposition.newly_failing)),
            tuple(sorted(proposition.flaky)),
        )

    def _validate_request(self, request: EvaluationRequest, candidate: SkillCandidate) -> None:
        if request.content_hash != candidate.candidate_digest:
            raise ValueError("request content hash does not match candidate")
        if request.baseline != (candidate.base_digest or "none"):
            raise ValueError("request baseline does not match candidate")
        if request.suite != self.suite.family or request.suite_version != self.suite.version:
            raise ValueError("request suite does not match evaluator")
        if request.budget_seconds <= 0:
            raise ValueError("evaluation budget must be positive")

    def _existing_report(self, request: EvaluationRequest) -> str | None:
        if not self.output_root.is_dir():
            return None
        for path in self.output_root.glob("*/report.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if document.get("request") == asdict(request):
                rebuild_evolution_report(path.parent)
                return path.parent.name
        return None


def load_evolution_suite(path: Path) -> EvolutionSuite:
    path = path.resolve()
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode("utf-8"))
    if document.get("schema") != "run-agent.evolution-suite":
        raise ValueError("Unsupported evolution suite schema")
    tasks: list[EvolutionSuiteTask] = []
    for item in document.get("tasks", []):
        split = item.get("split")
        if split not in {"train", "selection", "test"}:
            raise ValueError(f"Invalid evolution split: {split}")
        task_path = (path.parent / str(item["path"])).resolve()
        tasks.append(EvolutionSuiteTask(str(item["id"]), split, str(item["topic"]), task_path))
    if not tasks or any(
        sum(task.split == split for task in tasks) != 3 for split in ("train", "selection", "test")
    ):
        raise ValueError("Evolution suite must contain exactly three tasks per split")
    return EvolutionSuite(
        path,
        str(document.get("version") or ""),
        str(document.get("family") or ""),
        tuple(tasks),
        _sha256(raw),
    )


def reduce_evolution_trials(trials: list[EvolutionTrial], *, repeats: int) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[EvolutionTrial]] = {}
    for trial in trials:
        grouped.setdefault((trial.split, trial.task_id, trial.arm), []).append(trial)
    tasks: dict[str, dict[str, Any]] = {}
    infra_errors = 0
    required_passes = repeats // 2 + 1
    for (split, task_id, arm), rows in sorted(grouped.items()):
        passes = sum(row.succeeded for row in rows)
        errors = sum(row.infrastructure_error is not None for row in rows)
        infra_errors += errors
        task = tasks.setdefault(task_id, {"split": split})
        task[arm] = {
            "passes": passes,
            "trials": len(rows),
            "errors": errors,
            "passed": errors == 0 and passes >= required_passes,
        }

    regressions: list[str] = []
    improvements: list[str] = []
    for task_id, row in tasks.items():
        if row["split"] != "selection":
            continue
        baseline = bool(row["baseline"]["passed"])
        candidate = bool(row["candidate"]["passed"])
        if baseline and not candidate:
            regressions.append(task_id)
        if not baseline and candidate:
            improvements.append(task_id)
    gate = {
        "passed": infra_errors == 0 and not regressions and bool(improvements),
        "infrastructure_errors": infra_errors,
        "regressions": regressions,
        "improvements": improvements,
        "rule": (
            f"{required_passes}-of-{repeats}; no selection regression and at least one improvement"
        ),
    }
    return {
        "tasks": tasks,
        "selection_gate": gate,
        "efficiency": _efficiency(trials),
    }


def _efficiency(trials: list[EvolutionTrial]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ("baseline", "candidate"):
        rows = [trial for trial in trials if trial.arm == arm]
        costs = [trial.metadata.get("cost") for trial in rows]
        complete = bool(rows) and all(isinstance(value, int | float) for value in costs)
        summary[arm] = {
            "trials": len(rows),
            "calls": sum(_number(row.metadata.get("calls")) for row in rows),
            "input_tokens": sum(_number(row.metadata.get("input_tokens")) for row in rows),
            "output_tokens": sum(_number(row.metadata.get("output_tokens")) for row in rows),
            "cache_read_tokens": sum(
                _number(row.metadata.get("cache_read_tokens")) for row in rows
            ),
            "cache_write_tokens": sum(
                _number(row.metadata.get("cache_write_tokens")) for row in rows
            ),
            "known_cost": sum(
                float(value)
                for row in rows
                if isinstance((value := row.metadata.get("known_cost")), int | float)
            ),
            "total_cost": sum(_float_cost(value) for value in costs) if complete else None,
        }
    return summary


def _number(value: JSONValue) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _float_cost(value: JSONValue) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def rebuild_evolution_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    inventory_path = root / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory.get("schema") != EVOLUTION_REPORT_SCHEMA:
        raise ValueError("Unsupported evolution inventory")
    for item in inventory.get("files", []):
        path = root / str(item["path"])
        if not path.is_file() or path.stat().st_size != int(item["size"]):
            raise ValueError(f"Evolution evidence missing or wrong size: {item['path']}")
        if _file_digest(path) != item["sha256"]:
            raise ValueError(f"Evolution evidence hash mismatch: {item['path']}")
    report = cast(dict[str, Any], json.loads((root / "report.json").read_text(encoding="utf-8")))
    if report.get("schema") != EVOLUTION_REPORT_SCHEMA:
        raise ValueError("Unsupported evolution report")
    source = cast(dict[str, Any], json.loads((root / "source.json").read_text(encoding="utf-8")))
    if source != report.get("source"):
        raise ValueError("Evolution source does not match report")
    trials = [
        _trial_from_json(item)
        for item in json.loads((root / "trials.json").read_text(encoding="utf-8"))
    ]
    rebuilt = reduce_evolution_trials(trials, repeats=int(source["repeats"]))
    if rebuilt != report.get("summary"):
        raise ValueError("Evolution report does not match frozen trials")
    return report


def _trial_json(trial: EvolutionTrial) -> dict[str, Any]:
    return {
        **asdict(trial),
        "metadata": dict(trial.metadata),
    }


def _trial_from_json(raw: Mapping[str, Any]) -> EvolutionTrial:
    return EvolutionTrial(
        task_id=str(raw["task_id"]),
        split=raw["split"],
        arm=raw["arm"],
        repeat=int(raw["repeat"]),
        succeeded=bool(raw["succeeded"]),
        infrastructure_error=raw.get("infrastructure_error"),
        duration_ms=float(raw["duration_ms"]),
        metadata=cast(Mapping[str, JSONValue], raw.get("metadata", {})),
        fail_to_pass=tuple(raw.get("fail_to_pass", ())),
        pass_to_pass=tuple(raw.get("pass_to_pass", ())),
        unmet_targets=tuple(raw.get("unmet_targets", ())),
        newly_failing=tuple(raw.get("newly_failing", ())),
        flaky=tuple(raw.get("flaky", ())),
    )


def _install_skill(state_root: Path, name: str, content: str | None) -> None:
    if state_root.exists():
        shutil.rmtree(state_root)
    state_root.mkdir(parents=True)
    if content is None:
        return
    target = state_root / "skills" / name / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(content.encode("utf-8"))


def _installed_digest(state_root: Path, name: str) -> str | None:
    path = state_root / "skills" / name / "SKILL.md"
    return _sha256(path.read_bytes()) if path.is_file() else None


def _digest_optional(content: str | None) -> str | None:
    return _sha256(content.encode()) if content is not None else None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _inventory(root: Path, *, exclude: set[str]) -> list[dict[str, JSONValue]]:
    rows: list[dict[str, JSONValue]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if (
            relative in exclude
            or relative.startswith("workspaces/")
            or relative.startswith("state/")
        ):
            continue
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": _file_digest(path)})
    return rows


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "CodingExecutor",
    "EVOLUTION_REPORT_SCHEMA",
    "EvolutionEvaluationService",
    "EvolutionSuite",
    "EvolutionSuiteTask",
    "EvolutionTrial",
    "TaskExecutor",
    "load_evolution_suite",
    "rebuild_evolution_report",
    "reduce_evolution_trials",
]
