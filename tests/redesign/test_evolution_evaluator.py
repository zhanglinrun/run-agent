from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from run_agent_coding.host.evaluation import EvaluationRequest
from run_agent_evals.evolution import (
    EvolutionEvaluationService,
    TaskExecutor,
    _trial_from_json,
    rebuild_evolution_report,
    reduce_evolution_trials,
)
from run_agent_evals.models import ExecutionCancelled, ExecutionResult
from run_agent_evals.task_spec import TaskSpec
from run_agent_extensions.experience.candidates import (
    CandidateOperation,
    SkillCandidateStore,
)
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


class ReferenceWhenSkilled:
    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> dict[str, object]:
        skilled = any((state_root / "skills").glob("*/SKILL.md"))
        if skilled:
            for source in spec.reference.iterdir():
                target = workspace / source.name
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
        return {
            "calls": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "known_cost": 0.01,
            "cost": 0.01,
        }


class SleepsPastTheWatchdog:
    """Blocks far past a tiny budget and answers the watchdog like the executor does."""

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> dict[str, object]:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise ExecutionCancelled(ExecutionResult(output="watchdog cancelled")) from None
        raise AssertionError("the watchdog never cancelled this trial")


class CancelsBelowTheBudget:
    """Raises the executor's cancellation form long before this trial's own budget."""

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> dict[str, object]:
        raise ExecutionCancelled(ExecutionResult(output="campaign interrupted"))


def _service(
    tmp_path: Path,
    *,
    executor: TaskExecutor | None = None,
    repeats: int = 3,
) -> tuple[EvolutionEvaluationService, str]:
    candidates = SkillCandidateStore(tmp_path / "experience" / "candidates")
    skills = SkillManager(SkillRoots(user=tmp_path / "skills", project=tmp_path / "project-skills"))
    content = (
        "---\nname: config-skill\ndescription: solve config tasks\n---\nUse the project contract.\n"
    )
    candidate = candidates.create(
        scope="user",
        name="config-skill",
        source_session="session",
        source_run="run",
        base_content=None,
        operations=(CandidateOperation("add", new_text=content),),
        candidate_content=content,
    )
    service = EvolutionEvaluationService(
        suite=Path("evals/evolution/config.toml"),
        output_root=tmp_path / "reports",
        candidates=candidates,
        skills=skills,
        executor=executor if executor is not None else ReferenceWhenSkilled(),
        repeats=repeats,
    )
    return service, candidate.candidate_id


def _request(
    candidate_id: str, candidate_digest: str, *, budget_seconds: float = 30
) -> EvaluationRequest:
    return EvaluationRequest(
        candidate_id=candidate_id,
        content_hash=candidate_digest,
        baseline="none",
        suite="config",
        suite_version="2",
        budget_seconds=budget_seconds,
    )


def _rewrite_frozen(path: Path, payload: object) -> None:
    """Rewrite one frozen file and repair its inventory row, defeating hashing only."""
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.write_bytes(data)
    inventory_path = path.parent / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    for item in inventory["files"]:
        if item["path"] == path.name:
            item["size"] = len(data)
            item["sha256"] = hashlib.sha256(data).hexdigest()
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _trial_rows(root: Path) -> list[dict[str, object]]:
    rows = json.loads((root / "trials.json").read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    return rows


@pytest.mark.anyio
async def test_evolution_service_uses_hidden_graders_and_selection_gate(tmp_path: Path) -> None:
    service, candidate_id = _service(tmp_path)
    candidate = service.candidates.require(candidate_id)
    request = _request(candidate_id, candidate.candidate_digest)

    report_id = await service.submit(request)
    report = await service.report(report_id)

    assert report.passed is True
    gate = report.summary["selection_gate"]
    assert isinstance(gate, dict)
    assert len(gate["improvements"]) == 3
    assert gate["regressions"] == []
    tasks = report.summary["tasks"]
    assert isinstance(tasks, dict)
    assert all(
        task_id in tasks
        for task_id in (
            "config-nested-override",
            "config-null-vs-missing",
            "config-deprecation",
            "config-list-merge",
        )
    )
    efficiency = report.summary["efficiency"]
    assert isinstance(efficiency, dict)
    assert efficiency["candidate"]["total_cost"] is not None


@pytest.mark.anyio
async def test_evolution_report_rebuild_rejects_tampering(tmp_path: Path) -> None:
    service, candidate_id = _service(tmp_path)
    candidate = service.candidates.require(candidate_id)
    request = _request(candidate_id, candidate.candidate_digest)
    report_id = await service.submit(request)
    root = service.output_root / report_id
    assert rebuild_evolution_report(root)["passed"] is True

    trials = root / "trials.json"
    trials.write_text(trials.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch|wrong size"):
        rebuild_evolution_report(root)


@pytest.mark.anyio
async def test_a_watchdog_hit_is_a_failed_trial_not_an_infrastructure_error(
    tmp_path: Path,
) -> None:
    """A trial that ends at its own budget is a failure, never a poisoned gate."""
    service, candidate_id = _service(tmp_path, executor=SleepsPastTheWatchdog(), repeats=1)
    candidate = service.candidates.require(candidate_id)
    request = _request(candidate_id, candidate.candidate_digest, budget_seconds=0.05)

    report_id = await service.submit(request)
    root = service.output_root / report_id
    rows = _trial_rows(root)

    assert len(rows) == 12
    assert all(row["timed_out"] is True for row in rows)
    assert all(row["infrastructure_error"] is None for row in rows)
    assert all(row["succeeded"] is False for row in rows)
    # The elapsed time really does sit on the 50 ms budget it was given.
    # The elapsed time really does sit on the 50 ms budget it was given. Windows timer
    # granularity (about 15 ms) can fire the deadline a little early, so the bound stays
    # well below the budget instead of hugging it.
    assert all(float(row["duration_ms"]) >= 20 for row in rows)

    report = await service.report(report_id)
    gate = report.summary["selection_gate"]
    assert isinstance(gate, dict)
    assert gate["infrastructure_errors"] == 0
    assert gate["passed"] is False
    tasks = report.summary["tasks"]
    assert isinstance(tasks, dict)
    assert len(tasks) == 6
    for row in tasks.values():
        assert isinstance(row, dict)
        for arm in ("baseline", "candidate"):
            measured = row[arm]
            assert isinstance(measured, dict)
            assert measured["trials"] == 1
            assert measured["passes"] == 0
            assert measured["errors"] == 0
            assert measured["passed"] is False
    assert rebuild_evolution_report(root)["passed"] is False


@pytest.mark.anyio
async def test_a_cancellation_below_the_budget_stays_an_infrastructure_error(
    tmp_path: Path,
) -> None:
    """A cold cancellation is not this trial's watchdog and still fails the gate."""
    service, candidate_id = _service(tmp_path, executor=CancelsBelowTheBudget(), repeats=1)
    candidate = service.candidates.require(candidate_id)
    request = _request(candidate_id, candidate.candidate_digest, budget_seconds=300)

    report_id = await service.submit(request)
    root = service.output_root / report_id
    rows = _trial_rows(root)

    assert len(rows) == 12
    assert all(row["timed_out"] is False for row in rows)
    assert all(row["succeeded"] is False for row in rows)
    assert all(str(row["infrastructure_error"]).startswith("ExecutionCancelled:") for row in rows)

    report = await service.report(report_id)
    gate = report.summary["selection_gate"]
    assert isinstance(gate, dict)
    assert gate["infrastructure_errors"] == 12
    assert gate["passed"] is False


@pytest.mark.anyio
async def test_trials_frozen_before_timed_out_still_reduce_and_rebuild(tmp_path: Path) -> None:
    """Rows written before the ``timed_out`` field default to False and stay rebuildable."""
    service, candidate_id = _service(tmp_path, repeats=1)
    candidate = service.candidates.require(candidate_id)
    report_id = await service.submit(_request(candidate_id, candidate.candidate_digest))
    root = service.output_root / report_id

    rows = _trial_rows(root)
    legacy = [{key: value for key, value in row.items() if key != "timed_out"} for row in rows]
    assert all("timed_out" not in row for row in legacy)
    _rewrite_frozen(root / "trials.json", legacy)

    document = rebuild_evolution_report(root)
    assert document["passed"] is True
    trials = [_trial_from_json(row) for row in legacy]
    assert all(trial.timed_out is False for trial in trials)
    assert reduce_evolution_trials(trials, repeats=1) == document["summary"]
