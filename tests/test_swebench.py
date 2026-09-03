from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import subprocess

import pytest

from agents.evaluation.swebench import (
    EXPECTED_ROWS,
    EXPECTED_SHA256,
    load_swebench_verified,
    policy_hash,
    sha256_file,
)
from agents.evaluation.swebench import adapter as adapter_module
from agents.evaluation.swebench import evaluator as evaluator_module
from agents.evaluation.swebench import runner as runner_module


def test_swebench_verified_dataset_is_pinned_and_complete() -> None:
    root = Path(__file__).resolve().parents[1]
    dataset = root / "data" / "SWE-bench_Verified" / "test-00000-of-00001.parquet"
    instances = load_swebench_verified(dataset)

    assert len(instances) == EXPECTED_ROWS == 500
    assert sha256_file(dataset) == EXPECTED_SHA256
    assert len({item.instance_id for item in instances}) == EXPECTED_ROWS


def test_swebench_prompt_does_not_leak_gold_patch_or_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    instance = load_swebench_verified(root / "data" / "SWE-bench_Verified" / "test-00000-of-00001.parquet")[0]
    prompt = instance.prompt()

    assert instance.problem_statement in prompt
    assert instance.gold_patch not in prompt
    assert instance.test_patch not in prompt
    assert instance.eval_script not in prompt


def test_explicit_swebench_instance_order_is_preserved() -> None:
    instances = load_swebench_verified(
        Path(__file__).resolve().parents[1] / "data" / "SWE-bench_Verified" / "test-00000-of-00001.parquet"
    )
    wanted = [instances[4].instance_id, instances[1].instance_id, instances[3].instance_id]

    selected = adapter_module.select_instances(
        instances,
        limit=None,
        seed=42,
        instance_ids=wanted,
    )

    assert [item.instance_id for item in selected] == wanted


def test_swebench_policy_hash_is_reproducible() -> None:
    root = Path(__file__).resolve().parents[1]
    first = policy_hash(root)
    assert first
    assert first == policy_hash(root)


def test_focused_verification_extracts_only_eval_test_command() -> None:
    instance = SimpleNamespace(
        eval_script="""#!/bin/bash
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .
./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 app.tests
git apply -v - <<'EOF'
hidden test patch
EOF
""",
    )

    commands = runner_module._focused_verification_commands(instance)

    assert commands is not None
    assert commands[0].name == "benchmark-focused-tests"
    assert commands[0].argv[1:] == (
        "tests/runtests.py",
        "--verbosity",
        "2",
        "--settings=test_sqlite",
        "--parallel",
        "1",
        "app.tests",
    )


def test_swebench_loader_rejects_missing_fields_and_duplicate_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = tmp_path / "dataset.parquet"
    dataset.write_bytes(b"fixture")

    class FakeTable:
        def __init__(self, rows):
            self.rows = rows

        def to_pylist(self):
            return self.rows

    class FakeParquet:
        rows = []

        @classmethod
        def read_table(cls, _path):
            return FakeTable(cls.rows)

    monkeypatch.setattr(adapter_module, "_require_pyarrow", lambda: FakeParquet)
    FakeParquet.rows = [{"instance_id": "x", "repo": "owner/repo", "problem_statement": "fix"}]
    with pytest.raises(ValueError, match="base_commit"):
        adapter_module.load_swebench_verified(dataset)

    row = {
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": "abc123",
        "problem_statement": "fix",
    }
    FakeParquet.rows = [row, dict(row)]
    with pytest.raises(ValueError, match="duplicate"):
        adapter_module.load_swebench_verified(dataset)


def test_checkout_fetches_complete_base_commit_without_partial_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(root, *args, **kwargs):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(adapter_module, "_run_git", fake_git)
    instance = SimpleNamespace(
        instance_id="owner__repo-1",
        repo="owner/repo",
        base_commit="abc123",
    )

    adapter_module.checkout_instance(instance, tmp_path / "workspace")

    assert calls[0] == ("init", "-q")
    assert ("remote", "add", "origin", "https://github.com/owner/repo.git") in calls
    assert ("fetch", "--depth=1", "origin", "abc123") in calls
    assert all("--filter=blob:none" not in call for call in calls)

def test_official_evaluator_uses_argv_and_records_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("{}\n", encoding="utf-8")
    instance = load_swebench_verified(
        Path(__file__).resolve().parents[1] / "data" / "SWE-bench_Verified" / "test-00000-of-00001.parquet"
    )[0]
    seen: list[str] = []

    monkeypatch.setattr(evaluator_module.importlib.util, "find_spec", lambda _name: object())

    def fake_run(command, **kwargs):
        seen.extend(command)
        report_dir = Path(command[command.index("--report_dir") + 1])
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(
            json.dumps({"total_instances": 1, "resolved_instances": 0}),
            encoding="utf-8",
        )
        assert kwargs["check"] is False
        assert "shell" not in kwargs or kwargs["shell"] is False
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    report = evaluator_module.run_official_grader(
        predictions,
        [instance],
        tmp_path,
        SimpleNamespace(max_workers=1, eval_timeout=60),
    )

    assert report == tmp_path / "official-report" / "report.json"
    assert "--predictions_path" in seen
    assert "--instance_ids" in seen
    assert "--report_dir" in seen
    recorded = json.loads((tmp_path / "official-grader.command.json").read_text(encoding="utf-8"))
    assert recorded["argv"] == seen


def test_rewrite_scored_results_projects_official_statuses(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    rows = [
        {"instance_id": "resolved-case", "repair_attempts": 0},
        {"instance_id": "unresolved-case", "repair_attempts": 1},
        {"instance_id": "error-case", "repair_attempts": 0},
        {"instance_id": "empty-case", "repair_attempts": 0},
        {"instance_id": "unknown-case", "repair_attempts": 0},
    ]
    results.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = {
        "resolved_ids": ["resolved-case"],
        "unresolved_ids": ["unresolved-case"],
        "error_ids": ["error-case"],
        "empty_patch_ids": ["empty-case"],
        "completed_ids": ["resolved-case", "unresolved-case"],
    }

    updated = runner_module._rewrite_scored_results(results, report)

    by_id = {row["instance_id"]: row for row in updated}
    assert by_id["resolved-case"]["official_status"] == "resolved"
    assert by_id["resolved-case"]["correct"] is True
    assert by_id["unresolved-case"]["official_status"] == "unresolved"
    assert by_id["unresolved-case"]["correct"] is False
    assert by_id["error-case"]["official_status"] == "error"
    assert by_id["error-case"]["correct"] is False
    assert by_id["empty-case"]["official_status"] == "empty_patch"
    assert by_id["empty-case"]["correct"] is False
    assert by_id["unknown-case"]["official_status"] == "grader_completed"
    assert "correct" not in by_id["unknown-case"]
    assert [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()] == updated
    assert not results.with_suffix(".jsonl.tmp").exists()


def test_mechanism_report_records_verification_and_correction(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({
        "instance_id": "owner__repo-1",
        "patch_candidates": [{"patch_sha256": "a" * 64}],
        "verification_history": [{"outcome": "FAIL", "fingerprint": "b" * 16}],
        "correction_attempts": [{
            "attempt": 1,
            "action": "retry_in_place",
            "before_patch_sha256": "a" * 64,
            "after_patch_sha256": "c" * 64,
        }],
        "final_patch_sha256": "c" * 64,
        "official_status": "resolved",
        "failure": None,
    }) + "\n", encoding="utf-8")

    report = runner_module._write_mechanism_report(tmp_path, results)

    text = report.read_text(encoding="utf-8")
    assert "owner__repo-1" in text
    assert "retry_in_place" in text
    assert "resolved" in text


def test_mechanism_report_records_baseline_patch_without_candidates(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    patch_sha = "d" * 64
    results.write_text(json.dumps({
        "instance_id": "baseline-case",
        "final_patch_sha256": patch_sha,
        "official_status": "resolved",
    }) + "\n", encoding="utf-8")

    report = runner_module._write_mechanism_report(tmp_path, results)

    text = report.read_text(encoding="utf-8")
    assert "baseline-case" in text
    assert f"`{patch_sha[:12]}`" in text


@pytest.mark.asyncio
async def test_pi_rewrite_ablation_runs_fixed_arms_and_writes_paired_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[SimpleNamespace] = []

    async def fake_campaign(args):
        calls.append(args)
        run_dir = Path(args.output) / "run"
        run_dir.mkdir(parents=True)
        return run_dir

    monkeypatch.setattr(runner_module, "run_swebench_campaign", fake_campaign)
    monkeypatch.setattr(
        runner_module,
        "compare_predictions",
        lambda *args, **kwargs: {"cases": 5, "pass_at_1": {"absolute_delta": 0.0}},
    )

    output = await runner_module.run_pi_rewrite_ablation(SimpleNamespace(
        output=str(tmp_path),
        grade=True,
        model=None,
    ))

    assert output == tmp_path.resolve()
    assert [call.harness_mode for call in calls] == ["baseline", "verifier", "full"]
    assert all(call.model == "gpt-5.6-luna" for call in calls)
    assert all(call.instance_id == list(runner_module.PI_REWRITE_INSTANCES) for call in calls)
    report = json.loads((tmp_path / "paired_comparison.json").read_text(encoding="utf-8"))
    assert set(report["comparisons"]) == {
        "baseline_vs_verifier",
        "baseline_vs_full",
        "verifier_vs_full",
    }
