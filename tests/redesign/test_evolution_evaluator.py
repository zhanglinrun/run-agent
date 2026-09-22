from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from run_agent_coding.host.evaluation import EvaluationRequest
from run_agent_evals.evolution import EvolutionEvaluationService, rebuild_evolution_report
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


def _service(tmp_path: Path) -> tuple[EvolutionEvaluationService, str]:
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
        executor=ReferenceWhenSkilled(),
    )
    return service, candidate.candidate_id


@pytest.mark.anyio
async def test_evolution_service_uses_hidden_graders_and_selection_gate(tmp_path: Path) -> None:
    service, candidate_id = _service(tmp_path)
    candidate = service.candidates.require(candidate_id)
    request = EvaluationRequest(
        candidate_id=candidate_id,
        content_hash=candidate.candidate_digest,
        baseline="none",
        suite="config",
        suite_version="1",
        budget_seconds=30,
    )

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
    request = EvaluationRequest(
        candidate_id=candidate_id,
        content_hash=candidate.candidate_digest,
        baseline="none",
        suite="config",
        suite_version="1",
        budget_seconds=30,
    )
    report_id = await service.submit(request)
    root = service.output_root / report_id
    assert rebuild_evolution_report(root)["passed"] is True

    trials = root / "trials.json"
    trials.write_text(trials.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch|wrong size"):
        rebuild_evolution_report(root)
