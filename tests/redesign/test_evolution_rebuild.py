"""The ``evolve-rebuild`` CLI entry point: offline verification of frozen evidence.

These tests drive ``run_agent_evals.cli.main`` (the ``evolve-rebuild`` subcommand
registered in ``_parser`` and dispatched at the end of ``main``), not the internal
``rebuild_evolution_report`` helper, so the subcommand's argument handling, its
unique-report-child resolution and its failure surfacing are all exercised.

Everything is offline and deterministic: the frozen artifacts come from the same
fully-fake executor used by ``test_evolution_evaluator.py``, and no provider or model
is ever contacted. ``main`` maps a rejected rebuild onto a ``SystemExit``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.redesign.test_evolution_evaluator import ReferenceWhenSkilled

from run_agent_coding.host.evaluation import EvaluationRequest
from run_agent_evals.cli import main
from run_agent_evals.evolution import EvolutionEvaluationService
from run_agent_evals.task_spec import TaskSpec
from run_agent_extensions.experience.candidates import CandidateOperation, SkillCandidateStore
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


class StubCodingExecutor:
    """Offline stand-in for the Coding executor; no provider is ever contacted."""

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

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> dict[str, object]:
        return {
            "calls": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "known_cost": 0.0,
            "cost": 0.0,
            "state_root": str(state_root),
            "workspace": str(workspace),
        }


async def _frozen_evidence(tmp_path: Path) -> Path:
    """Produce one real frozen evolution report directory with a fake executor."""
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
    request = EvaluationRequest(
        candidate_id=candidate.candidate_id,
        content_hash=candidate.candidate_digest,
        baseline="none",
        suite="config",
        suite_version="2",
        budget_seconds=30,
    )
    report_id = await service.submit(request)
    return service.output_root / report_id


async def test_evolve_rebuild_cli_verifies_a_unique_report_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = await _frozen_evidence(tmp_path)
    # The parent holds exactly one report child, which the subcommand must resolve itself.
    assert main(["evolve-rebuild", str(root.parent)]) == 0
    output = capsys.readouterr().out
    assert f"Evidence verified: {root.resolve()}" in output
    assert '"selection_gate"' in output

    document = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert document["passed"] is True
    assert document["report_id"] == root.name


async def test_evolve_rebuild_cli_rejects_tampered_evidence(tmp_path: Path) -> None:
    root = await _frozen_evidence(tmp_path)
    assert main(["evolve-rebuild", str(root)]) == 0

    trials = root / "trials.json"
    trials.write_text(trials.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SystemExit) as failure:
        main(["evolve-rebuild", str(root)])
    # A tampered artifact must not produce a verified report, only a refusal.
    assert "Evaluation failed" in str(failure.value)
    assert "hash mismatch" in str(failure.value) or "wrong size" in str(failure.value)


def test_evolve_rebuild_cli_refuses_a_root_without_a_unique_report_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "reports"
    empty.mkdir()
    with pytest.raises(SystemExit) as failure:
        main(["evolve-rebuild", str(empty)])
    assert "evolve-rebuild needs a report directory or a unique report child" in str(failure.value)
    assert capsys.readouterr().out == ""


def test_evolve_cli_writes_an_arm_campaign_and_rebuilds_the_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A whole arm campaign runs offline and its comparison view is verified offline."""
    monkeypatch.setattr("run_agent_evals.cli.CodingExecutor", StubCodingExecutor)
    state = tmp_path / "state"
    output = tmp_path / "reports"
    assert (
        main(
            [
                "evolve",
                "evals/evolution/config.toml",
                "--skill",
                "config-skill",
                "--scope",
                "user",
                "--arm",
                "no-skill",
                "--concurrency",
                "2",
                "--state-root",
                str(state),
                "--output-root",
                str(output),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "comparison.json" in printed

    comparison = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    assert comparison["schema"] == "run-agent.evolution-comparison.v1"
    assert [arm["label"] for arm in comparison["arms"]] == ["no-skill"]
    assert (output / "REPORT.md").is_file()
    report_dir = output / comparison["arms"][0]["report"]
    trials = json.loads((report_dir / "trials.json").read_text(encoding="utf-8"))
    assert len(trials) == 18
    assert len({row["metadata"]["state_root"] for row in trials}) == 18
    assert len({row["metadata"]["workspace"] for row in trials}) == 18

    assert main(["evolve-rebuild", str(output)]) == 0
    verified = capsys.readouterr().out
    assert "Evidence verified" in verified
    assert '"no-skill"' in verified

    report_path = output / "REPORT.md"
    report_path.write_text(report_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(SystemExit) as failure:
        main(["evolve-rebuild", str(output)])
    assert "Evaluation failed" in str(failure.value)
    assert "comparison report does not match" in str(failure.value)


def test_evolve_cli_refuses_an_ablation_without_an_arm(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as failure:
        main(
            [
                "evolve",
                "evals/evolution/config.toml",
                "--skill",
                "config-skill",
                "--ablate",
                "project-probe",
                "--state-root",
                str(tmp_path / "state"),
                "--output-root",
                str(tmp_path / "reports"),
            ]
        )
    assert "--ablate" in str(failure.value)
