"""The four evolution arms and the two ablations: semantics, evidence, comparison.

Everything here is offline: ``ArmProbeExecutor`` stands in for the Coding session, so
no provider or model is ever contacted. The hidden graders still run for real, which
is why these reports are frozen with ``repeats=1``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from run_agent_coding.host.evaluation import EvaluationRequest
from run_agent_evals.evolution import (
    EVOLUTION_SCOPE_STATEMENT,
    EvolutionArmEvaluationService,
    EvolutionArmRefused,
    EvolutionArmReport,
    EvolutionArmRequest,
    EvolutionEvaluationService,
    rebuild_evolution_comparison,
    rebuild_evolution_report,
    write_evolution_comparison,
)
from run_agent_evals.task_spec import TaskSpec
from run_agent_extensions.experience.candidates import (
    CandidateOperation,
    ProjectProbe,
    SkillCandidate,
    SkillCandidateStore,
    capture_claims,
)
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots

SUITE = Path("evals/evolution/config.toml")
SKILL = "config-skill"
FORMAL = (
    "---\nname: config-skill\ndescription: solve config tasks\ncreated_by: evolution\n---\n"
    "Use the project contract.\n"
)
REVISION = FORMAL.replace("Use the project contract.", "Use the revised project contract.")
DISALLOWED = "this file has no frontmatter\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArmProbeExecutor:
    """Copies the reference only when the installed SKILL.md solves this task family."""

    def __init__(self, solving_digest: str | None) -> None:
        self.solving_digest = solving_digest

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> dict[str, Any]:
        skill = state_root / "skills" / SKILL / "SKILL.md"
        installed = _digest(skill.read_text(encoding="utf-8")) if skill.is_file() else None
        if installed is not None and installed == self.solving_digest:
            for source in sorted(spec.reference.iterdir()):
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
            "state_root": str(state_root),
            "workspace": str(workspace),
            "instruction": spec.instruction,
            "installed_skill_digest": installed,
        }


def _skills(tmp_path: Path, *, formal: str | None = FORMAL) -> SkillManager:
    roots = SkillRoots(user=tmp_path / "skills", project=tmp_path / "project-skills")
    if formal is not None:
        target = roots.user / SKILL / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(formal, encoding="utf-8")
    return SkillManager(roots)


def _candidates(tmp_path: Path) -> SkillCandidateStore:
    return SkillCandidateStore(tmp_path / "experience" / "candidates")


def _arm_service(
    tmp_path: Path,
    *,
    solving_digest: str | None,
    skills: SkillManager | None = None,
    candidates: SkillCandidateStore | None = None,
    probe: ProjectProbe | None = None,
    repeats: int = 1,
    concurrency: int = 1,
) -> EvolutionArmEvaluationService:
    return EvolutionArmEvaluationService(
        suite=SUITE,
        output_root=tmp_path / "reports",
        candidates=candidates if candidates is not None else _candidates(tmp_path),
        skills=skills if skills is not None else _skills(tmp_path),
        executor=ArmProbeExecutor(solving_digest),
        repeats=repeats,
        concurrency=concurrency,
        probe=probe,
    )


def _revision_candidate(candidates: SkillCandidateStore) -> SkillCandidate:
    return candidates.create(
        scope="user",
        name=SKILL,
        source_session="session",
        source_run="run",
        base_content=FORMAL,
        operations=(
            CandidateOperation(
                "replace",
                old_text="Use the project contract.",
                new_text="Use the revised project contract.",
            ),
        ),
        candidate_content=REVISION,
    )


def _invalid_candidate(candidates: SkillCandidateStore) -> SkillCandidate:
    return candidates.create(
        scope="user",
        name=SKILL,
        source_session="session",
        source_run="run",
        base_content=None,
        operations=(CandidateOperation("add", new_text=DISALLOWED),),
        candidate_content=DISALLOWED,
    )


async def _paired_report_id(
    tmp_path: Path,
    candidates: SkillCandidateStore,
    skills: SkillManager,
    candidate: SkillCandidate,
) -> str:
    product = EvolutionEvaluationService(
        suite=SUITE,
        output_root=tmp_path / "reports",
        candidates=candidates,
        skills=skills,
        executor=ArmProbeExecutor(_digest(REVISION)),
        repeats=1,
    )
    return await product.submit(
        EvaluationRequest(
            candidate_id=candidate.candidate_id,
            content_hash=candidate.candidate_digest,
            baseline=candidate.base_digest or "none",
            suite="config",
            suite_version="2",
            budget_seconds=30,
        )
    )


def _trials(report: EvolutionArmReport) -> list[dict[str, Any]]:
    loaded = json.loads((report.root / "trials.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    return loaded


def _rewrite(path: Path, payload: object) -> None:
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


@pytest.mark.anyio
async def test_no_skill_installs_nothing_and_static_skill_installs_the_formal_content(
    tmp_path: Path,
) -> None:
    service = _arm_service(tmp_path, solving_digest=_digest(FORMAL))

    no_skill = await service.evaluate(EvolutionArmRequest(arm="no-skill", skill=SKILL))
    rows = _trials(no_skill)
    assert no_skill.passed is True
    assert len(rows) == 6
    assert {row["split"] for row in rows} == {"selection", "test"}
    assert all(row["metadata"]["installed_skill_digest"] is None for row in rows)
    assert all(row["metadata"]["evaluated_skill_digest"] is None for row in rows)
    assert all(not row["metadata"]["instruction"].startswith("/skill:") for row in rows)
    assert all(row["succeeded"] is False for row in rows)
    assert no_skill.source["arm"] == "no-skill"
    assert no_skill.source["ablation"] is None
    assert no_skill.source["checks"] == {"structure": False, "facts": False, "behavior_gate": False}
    assert len(no_skill.source["task_digests"]) == 9

    static = await service.evaluate(EvolutionArmRequest(arm="static-skill", skill=SKILL))
    rows = _trials(static)
    assert all(row["metadata"]["installed_skill_digest"] == _digest(FORMAL) for row in rows)
    assert all(row["metadata"]["evaluated_skill_digest"] == _digest(FORMAL) for row in rows)
    assert all(row["metadata"]["instruction"].startswith(f"/skill:{SKILL} ") for row in rows)
    assert all(row["succeeded"] for row in rows)
    assert static.source["base_digest"] == _digest(FORMAL)
    assert static.source["candidate_id"] is None
    assert static.source["candidate_digest"] is None
    assert rebuild_evolution_report(static.root)["passed"] is True


@pytest.mark.anyio
async def test_gated_evolution_needs_a_verified_passing_paired_report(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    candidate = _revision_candidate(candidates)
    service = _arm_service(
        tmp_path, solving_digest=_digest(REVISION), skills=skills, candidates=candidates
    )
    request = EvolutionArmRequest(
        arm="gated-evolution", skill=SKILL, candidate_id=candidate.candidate_id
    )

    with pytest.raises(EvolutionArmRefused, match="passing paired"):
        await service.evaluate(request)

    paired = await _paired_report_id(tmp_path, candidates, skills, candidate)
    report = await service.evaluate(request)

    assert report.source["behavior_gate_report"] == paired
    assert report.source["checks"] == {"structure": True, "facts": True, "behavior_gate": True}
    assert report.source["candidate_digest"] == candidate.candidate_digest
    assert report.source["base_digest"] == _digest(FORMAL)
    rows = _trials(report)
    assert all(
        row["metadata"]["evaluated_skill_digest"] == candidate.candidate_digest for row in rows
    )
    assert all(row["succeeded"] for row in rows)
    assert rebuild_evolution_report(report.root)["passed"] is True


@pytest.mark.anyio
async def test_ungated_revision_installs_without_structure_or_fact_checks(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    invalid = _invalid_candidate(candidates)
    service = _arm_service(
        tmp_path, solving_digest=_digest(DISALLOWED), skills=skills, candidates=candidates
    )

    with pytest.raises(EvolutionArmRefused, match="structure check"):
        await service.evaluate(
            EvolutionArmRequest(
                arm="gated-evolution", skill=SKILL, candidate_id=invalid.candidate_id
            )
        )

    report = await service.evaluate(
        EvolutionArmRequest(arm="ungated-revision", skill=SKILL, candidate_id=invalid.candidate_id)
    )
    assert report.document["non_product"] is True
    assert report.source["non_product"] is True
    assert report.source["checks"] == {"structure": False, "facts": False, "behavior_gate": False}
    rows = _trials(report)
    assert all(
        row["metadata"]["evaluated_skill_digest"] == invalid.candidate_digest for row in rows
    )
    assert all(row["succeeded"] for row in rows)


@pytest.mark.anyio
async def test_project_probe_ablation_refuses_candidates_that_cite_project_facts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "settings.py").write_text("MODE = 'strict'\n", encoding="utf-8")
    probe = ProjectProbe(project, trusted=True)
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    cited = candidates.create(
        scope="user",
        name=SKILL,
        source_session="session",
        source_run="run",
        base_content=FORMAL,
        operations=(
            CandidateOperation(
                "replace",
                old_text="Use the project contract.",
                new_text="Read settings.py for the mode.",
            ),
        ),
        claims=capture_claims(probe, [("the project runs in strict mode", ["settings.py"])]),
        candidate_content=FORMAL.replace(
            "Use the project contract.", "Read settings.py for the mode."
        ),
    )
    service = _arm_service(
        tmp_path,
        solving_digest=_digest(REVISION),
        skills=skills,
        candidates=candidates,
        probe=probe,
    )

    with pytest.raises(EvolutionArmRefused, match="project facts"):
        await service.evaluate(
            EvolutionArmRequest(
                arm="gated-evolution",
                ablation="project-probe",
                skill=SKILL,
                candidate_id=cited.candidate_id,
            )
        )

    # A candidate that cites nothing passes the ablation and fails only on the gate.
    plain = _revision_candidate(candidates)
    with pytest.raises(EvolutionArmRefused, match="passing paired"):
        await service.evaluate(
            EvolutionArmRequest(
                arm="gated-evolution",
                ablation="project-probe",
                skill=SKILL,
                candidate_id=plain.candidate_id,
            )
        )


@pytest.mark.anyio
async def test_behavior_gate_ablation_skips_only_the_paired_gate(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    revision = _revision_candidate(candidates)
    service = _arm_service(
        tmp_path, solving_digest=_digest(REVISION), skills=skills, candidates=candidates
    )

    with pytest.raises(EvolutionArmRefused, match="passing paired"):
        await service.evaluate(
            EvolutionArmRequest(
                arm="gated-evolution", skill=SKILL, candidate_id=revision.candidate_id
            )
        )

    report = await service.evaluate(
        EvolutionArmRequest(
            arm="gated-evolution",
            ablation="behavior-gate",
            skill=SKILL,
            candidate_id=revision.candidate_id,
        )
    )
    assert report.source["ablation"] == "behavior-gate"
    assert report.source["label"] == "gated-evolution-behavior-gate"
    assert report.source["checks"] == {"structure": True, "facts": True, "behavior_gate": False}
    assert report.source["behavior_gate_report"] is None
    assert all(row["succeeded"] for row in _trials(report))

    # Unlike ungated-revision, the structure and fact checks still refuse here.
    invalid = _invalid_candidate(candidates)
    with pytest.raises(EvolutionArmRefused, match="structure check"):
        await service.evaluate(
            EvolutionArmRequest(
                arm="gated-evolution",
                ablation="behavior-gate",
                skill=SKILL,
                candidate_id=invalid.candidate_id,
            )
        )


@pytest.mark.anyio
async def test_arm_requests_refuse_mixed_or_missing_inputs(tmp_path: Path) -> None:
    service = _arm_service(tmp_path, solving_digest=None)

    with pytest.raises(EvolutionArmRefused, match="does not install a candidate"):
        await service.evaluate(
            EvolutionArmRequest(arm="no-skill", skill=SKILL, candidate_id="deadbeef")
        )
    with pytest.raises(EvolutionArmRefused, match="installed Skill"):
        await service.evaluate(EvolutionArmRequest(arm="static-skill", skill="missing-skill"))
    with pytest.raises(EvolutionArmRefused, match="candidate id"):
        await service.evaluate(EvolutionArmRequest(arm="gated-evolution", skill=SKILL))
    with pytest.raises(EvolutionArmRefused, match="applies only to gated-evolution"):
        await service.evaluate(
            EvolutionArmRequest(arm="no-skill", skill=SKILL, ablation="behavior-gate")
        )


@pytest.mark.anyio
async def test_every_arm_report_rebuilds_and_rejects_tampering(tmp_path: Path) -> None:
    service = _arm_service(tmp_path, solving_digest=_digest(FORMAL))
    report = await service.evaluate(EvolutionArmRequest(arm="no-skill", skill=SKILL))
    root = report.root
    assert rebuild_evolution_report(root)["passed"] is True

    trials_path = root / "trials.json"
    rows = _trials(report)
    trials_path.write_text(trials_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch|wrong size"):
        rebuild_evolution_report(root)

    _rewrite(trials_path, rows)
    assert rebuild_evolution_report(root)["passed"] is True

    report_path = root / "report.json"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    document["passed"] = not document["passed"]
    _rewrite(report_path, document)
    with pytest.raises(ValueError, match="pass flag does not match"):
        rebuild_evolution_report(root)

    _rewrite(report_path, {**document, "passed": True})
    assert rebuild_evolution_report(root)["passed"] is True

    flipped = [{**rows[0], "succeeded": not rows[0]["succeeded"]}, *rows[1:]]
    _rewrite(trials_path, flipped)
    with pytest.raises(ValueError, match="does not match frozen trials"):
        rebuild_evolution_report(root)


@pytest.mark.anyio
async def test_comparison_report_is_written_and_rebuildable(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    revision = _revision_candidate(candidates)
    service = _arm_service(
        tmp_path, solving_digest=_digest(REVISION), skills=skills, candidates=candidates
    )
    reports = [
        await service.evaluate(EvolutionArmRequest(arm="no-skill", skill=SKILL)),
        await service.evaluate(
            EvolutionArmRequest(
                arm="ungated-revision", skill=SKILL, candidate_id=revision.candidate_id
            )
        ),
    ]
    root = service.output_root
    comparison = write_evolution_comparison(root, reports)

    assert (root / "comparison.json").is_file()
    assert (root / "REPORT.md").is_file()
    assert comparison["scope_statement"] == EVOLUTION_SCOPE_STATEMENT
    arms = comparison["arms"]
    assert [arm["label"] for arm in arms] == ["no-skill", "ungated-revision"]
    assert arms[0]["totals"] == {
        "tasks": 6,
        "tasks_passed": 0,
        "trials": 6,
        "passes": 0,
        "errors": 0,
        "failed": 6,
    }
    assert arms[1]["totals"]["passes"] == 6
    assert arms[1]["non_product"] is True
    assert all(row["failed"] == row["trials"] - row["passes"] for row in arms[0]["tasks"])
    assert arms[1]["efficiency"]["calls"] == 6
    for arm in arms:
        assert (root / arm["report"] / "report.json").is_file()

    text = (root / "REPORT.md").read_text(encoding="utf-8")
    assert "不外推通用 Coding 能力" in text
    assert "不预设任何提升百分比" in text
    assert "| split | task | passes | trials | errors | failed | passed |" in text
    assert rebuild_evolution_comparison(root) == comparison

    (root / "REPORT.md").write_text(text + "tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="comparison report does not match"):
        rebuild_evolution_comparison(root)


@pytest.mark.anyio
async def test_concurrent_arm_trials_keep_isolated_state_roots(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    skills = _skills(tmp_path)
    revision = _revision_candidate(candidates)
    service = _arm_service(
        tmp_path,
        solving_digest=_digest(REVISION),
        skills=skills,
        candidates=candidates,
        concurrency=2,
    )
    report = await service.evaluate(
        EvolutionArmRequest(arm="ungated-revision", skill=SKILL, candidate_id=revision.candidate_id)
    )

    assert report.source["concurrency"] == 2
    rows = _trials(report)
    state_roots = [Path(row["metadata"]["state_root"]) for row in rows]
    workspaces = [Path(row["metadata"]["workspace"]) for row in rows]
    assert len(state_roots) == len(workspaces) == 6
    assert len(set(state_roots)) == 6
    assert len(set(workspaces)) == 6
    for row, state_root in zip(rows, state_roots, strict=True):
        assert row["metadata"]["installed_skill_digest"] == revision.candidate_digest
        assert row["metadata"]["evaluated_skill_digest"] == revision.candidate_digest
        assert (state_root / "skills" / SKILL / "SKILL.md").is_file()
        assert state_root.name == str(row["repeat"])
        assert row["task_id"] in state_root.parts
        assert state_root.is_relative_to(report.root)
    assert rebuild_evolution_report(report.root)["passed"] is True
