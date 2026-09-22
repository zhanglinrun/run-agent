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
from run_agent_extensions.experience.candidates import CandidateOperation, SkillCandidateStore
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


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
        suite_version="1",
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
