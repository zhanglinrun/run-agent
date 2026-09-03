from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.evaluation.compare import compare_predictions, mcnemar_exact_p_value


def _write_predictions(path: Path, outcomes: list[bool]) -> None:
    rows = []
    for index, correct in enumerate(outcomes):
        rows.append(
            {
                "case": {"case_id": f"c{index}"},
                "reference": "x",
                "correct": correct,
                "tokens": {"input": 10 + index, "output": 2},
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_paired_comparison_reports_delta_and_discordance(tmp_path: Path) -> None:
    baseline = tmp_path / "base.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_predictions(baseline, [True, False, False, True])
    _write_predictions(candidate, [True, True, False, False])

    report = compare_predictions(baseline, candidate, bootstrap_samples=500, seed=7)

    assert report["pass_at_1"]["absolute_delta"] == 0.0
    assert report["mcnemar"]["baseline_only"] == 1
    assert report["mcnemar"]["candidate_only"] == 1
    assert report["mcnemar"]["exact_two_sided_p"] == 1.0


def test_comparison_requires_identical_case_ids(tmp_path: Path) -> None:
    baseline = tmp_path / "base.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _write_predictions(baseline, [True, False])
    _write_predictions(candidate, [True])
    with pytest.raises(ValueError, match="identical case ids"):
        compare_predictions(baseline, candidate)


def test_mcnemar_all_candidate_wins_is_small() -> None:
    assert mcnemar_exact_p_value(0, 6) == pytest.approx(0.03125)


def test_compare_accepts_scored_swebench_results(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    baseline.write_text(
        "\n".join([
            json.dumps({"instance_id": "repo__one-1", "correct": False, "tokens": {"input": 10, "output": 5}}),
            json.dumps({"instance_id": "repo__two-2", "correct": True, "tokens": {"input": 10, "output": 5}}),
        ]) + "\n",
        encoding="utf-8",
    )
    candidate.write_text(
        "\n".join([
            json.dumps({"instance_id": "repo__one-1", "correct": True, "tokens": {"input": 11, "output": 6}}),
            json.dumps({"instance_id": "repo__two-2", "correct": True, "tokens": {"input": 11, "output": 6}}),
        ]) + "\n",
        encoding="utf-8",
    )

    report = compare_predictions(baseline, candidate, bootstrap_samples=200, seed=7)

    assert report["cases"] == 2
    assert report["pass_at_1"]["absolute_delta"] == 0.5


def test_compare_rejects_ungraded_swebench_results(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    baseline.write_text(json.dumps({"instance_id": "repo__one-1"}) + "\n", encoding="utf-8")
    candidate.write_text(json.dumps({"instance_id": "repo__one-1"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="official grader"):
        compare_predictions(baseline, candidate)
