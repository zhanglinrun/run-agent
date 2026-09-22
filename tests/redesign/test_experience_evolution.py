"""Evaluation-gated ownership, verification and publication tests."""

from dataclasses import dataclass

import pytest

from run_agent_coding.host.evaluation import (
    EvaluationReport,
    EvaluationRequest,
    UnavailableEvaluation,
)
from run_agent_coding.host.learning import LearningWritebackDisabled, writeback_disabled
from run_agent_extensions.experience.candidates import (
    CandidateError,
    CandidateOperation,
    ProjectProbe,
    SkillCandidateStore,
)
from run_agent_extensions.experience.evolution import SkillEvolution
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


def skill_text(name: str, body: str = "Run tests.", *, owner: str = "evolution") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Work with {name} safely.\n"
        f"created_by: {owner}\n"
        "---\n\n"
        f"# {name.title()}\n\n## Procedure\n{body}\n"
    )


class History:
    async def read_completed_run(self, run_id):
        if run_id != "run-1":
            raise ValueError("unknown completed run")
        return (object(),)


@dataclass
class Evaluation:
    passed: bool = True
    measured_hash: str | None = None
    available: bool = True
    request: EvaluationRequest | None = None

    async def submit(self, request: EvaluationRequest) -> str:
        self.request = request
        return "report-1"

    async def report(self, report_id: str) -> EvaluationReport:
        assert report_id == "report-1"
        assert self.request is not None
        return EvaluationReport(
            report_id=report_id,
            request=self.request,
            measured_content_hash=self.measured_hash or self.request.content_hash,
            passed=self.passed,
            summary={"trials": 3},
        )


def make_evolution(tmp_path, evaluation=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    user = tmp_path / "user-skills"
    skills = tmp_path / "project-skills"
    manager = SkillManager(SkillRoots(user, skills), guard=True, ledger=True)
    evolution = SkillEvolution(
        candidates=SkillCandidateStore(tmp_path / "candidate-store"),
        skills=manager,
        probe=ProjectProbe(project, trusted=True),
        evaluation=evaluation or UnavailableEvaluation(),
        project_enabled=True,
        history=History(),
    )
    return evolution, manager, project


async def propose_new(evolution, name="deploy", claims=()):
    content = skill_text(name)
    return await evolution.propose(
        scope="project",
        name=name,
        source_session="session-1",
        source_run="run-1",
        operations=(CandidateOperation("add", new_text=content),),
        candidate_content=content,
        claims=claims,
    )


async def test_unavailable_evaluation_keeps_candidate_cold_and_formal_skill_absent(tmp_path):
    evolution, manager, _ = make_evolution(tmp_path)
    candidate = await propose_new(evolution)
    assert candidate.status == "cold" and candidate.report_id is None
    assert manager.find("project", "deploy") is None
    with pytest.raises(CandidateError, match="no EvaluationService"):
        await evolution.publish(candidate.candidate_id)
    assert manager.find("project", "deploy") is None


async def test_evaluation_writeback_mode_cannot_refresh_or_reconcile_candidates(tmp_path):
    evolution, _, _ = make_evolution(tmp_path)
    candidate = await propose_new(evolution)
    evolution.evaluation = Evaluation()
    with writeback_disabled():
        assert evolution.reconcile() == []
        with pytest.raises(LearningWritebackDisabled):
            await evolution.evaluate(candidate.candidate_id)
    unchanged = evolution.candidates.require(candidate.candidate_id)
    assert unchanged.status == "cold" and unchanged.report_id is None


async def test_passed_report_publishes_one_skill_and_records_provenance_and_rollback(tmp_path):
    evaluation = Evaluation()
    evolution, manager, project = make_evolution(tmp_path, evaluation)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    candidate = await propose_new(
        evolution,
        claims=(("The project uses pytest configuration.", ("pyproject.toml",)),),
    )
    assert candidate.status == "verified"
    assert manager.find("project", "deploy") is None

    result = await evolution.publish(candidate.candidate_id)
    formal = manager.find("project", "deploy") / "SKILL.md"
    assert formal.read_text(encoding="utf-8") == skill_text("deploy")
    assert evolution.candidates.require(candidate.candidate_id).status == "published"
    entry = manager.ledger["project"].entries(limit=1)[0]
    assert entry.id == result.ledger_id and entry.actor == "evolution"
    assert entry.evidence["candidate_id"] == candidate.candidate_id
    assert entry.evidence["report_id"] == "report-1"
    assert entry.evidence["source_run"] == "run-1"
    assert entry.evidence["probes"][0]["path"] == "pyproject.toml"

    with manager.write_scope("project"):
        ok, message = manager.ledger["project"].rollback(entry.id)
    assert ok, message
    assert not formal.exists()


async def test_publication_rejects_a_tampered_candidate_blob(tmp_path):
    evolution, manager, _ = make_evolution(tmp_path, Evaluation())
    candidate = await propose_new(evolution)
    blob = evolution.candidates.blobs / f"{candidate.candidate_digest}.md"
    blob.write_text("tampered", encoding="utf-8")
    with pytest.raises(CandidateError, match="blob digest mismatch"):
        await evolution.publish(candidate.candidate_id)
    assert manager.find("project", "deploy") is None


async def test_publication_revalidates_probe_and_base_digests(tmp_path):
    evaluation = Evaluation()
    evolution, manager, project = make_evolution(tmp_path, evaluation)
    fact = project / "config.toml"
    fact.write_text("mode = 'safe'\n", encoding="utf-8")
    candidate = await propose_new(
        evolution,
        name="new-skill",
        claims=(("The project enables safe mode.", ("config.toml",)),),
    )
    fact.write_text("mode = 'changed'\n", encoding="utf-8")
    with pytest.raises(CandidateError, match="probe drifted"):
        await evolution.publish(candidate.candidate_id)
    assert manager.find("project", "new-skill") is None

    directory = manager.roots.project / "deploy"
    directory.mkdir(parents=True)
    original = skill_text("deploy")
    path = directory / "SKILL.md"
    path.write_text(original, encoding="utf-8")
    revised = original.replace("Run tests.", "Run tests twice.")
    candidate = await evolution.propose(
        scope="project",
        name="deploy",
        source_session="session-1",
        source_run="run-1",
        operations=(
            CandidateOperation("replace", old_text="Run tests.", new_text="Run tests twice."),
        ),
        candidate_content=revised,
    )
    path.write_text(original.replace("# Deploy", "# Deploy changed"), encoding="utf-8")
    with pytest.raises(CandidateError, match="base digest drifted"):
        await evolution.publish(candidate.candidate_id)
    assert "Run tests twice" not in path.read_text(encoding="utf-8")


async def test_existing_skill_requires_adoption_and_pinned_skill_is_refused(tmp_path):
    evolution, manager, _ = make_evolution(tmp_path, Evaluation())
    directory = manager.roots.project / "deploy"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    user_owned = skill_text("deploy", owner="user")
    path.write_text(user_owned, encoding="utf-8")
    operation = CandidateOperation("replace", old_text="Run tests.", new_text="Run checks.")

    with pytest.raises(CandidateError, match="user-owned"):
        await evolution.propose(
            scope="project",
            name="deploy",
            source_session="session-1",
            source_run="run-1",
            operations=(operation,),
        )
    adopted = evolution.adopt("project", "deploy")
    assert adopted.ledger_id and "created_by: evolution" in path.read_text(encoding="utf-8")
    manager.usage["project"].set_pinned("deploy", True)
    with pytest.raises(CandidateError, match="pinned"):
        await evolution.propose(
            scope="project",
            name="deploy",
            source_session="session-1",
            source_run="run-1",
            operations=(operation,),
        )


async def test_report_for_a_different_digest_never_verifies_or_publishes(tmp_path):
    evolution, manager, _ = make_evolution(tmp_path, Evaluation(measured_hash="wrong"))
    with pytest.raises(CandidateError, match="different candidate digest"):
        await propose_new(evolution)
    candidate = evolution.candidates.list()[0]
    assert candidate.status == "cold" and candidate.report_id == "report-1"
    assert manager.find("project", "deploy") is None
