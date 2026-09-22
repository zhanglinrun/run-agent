from __future__ import annotations

import json
from pathlib import Path

import pytest

from run_agent_evals.cli import main
from run_agent_evals.context_bench import rebuild_context_benchmark, run_context_benchmark


def test_context_benchmark_compares_strategies_and_rebuilds(tmp_path: Path) -> None:
    root = tmp_path / "context"

    report = run_context_benchmark(root)

    assert {path.name for path in root.glob("*.json")} == {
        "evidence.json",
        "inventory.json",
        "report.json",
    }
    assert report.summary["sample_count"] == 3
    strategies = report.summary["strategies"]
    summary_only = strategies["summary-only"]
    cheap_first = strategies["cheap-first"]
    assert summary_only["tokens_before"] == cheap_first["tokens_before"]
    assert summary_only["tokens_after"] == summary_only["tokens_before"]
    assert cheap_first["tokens_after"] < summary_only["tokens_after"]
    assert cheap_first["layer_counts"] == {"L1": 1, "L2": 1, "L3": 1}
    assert summary_only["artifact_count"] == 0
    assert cheap_first["artifact_count"] == 1
    assert summary_only["summary_needed_count"] == 3
    assert cheap_first["summary_needed_count"] == 0
    assert summary_only["protocol_pairing_all_valid"] is True
    assert cheap_first["protocol_pairing_all_valid"] is True
    assert cheap_first["prepare_latency_ms"]["p50"] >= 0

    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    profiles = {row["sample_id"]: row["profile"] for row in evidence["samples"]}
    assert profiles["large-tool-result"]["long_tool_results"] == 1
    assert profiles["parallel-tool-calls"]["parallel_tool_groups"] == 1
    assert profiles["multi-turn-history"]["turns"] == 5
    assert evidence["methodology"]["provider_requests"] == 0

    rebuilt = rebuild_context_benchmark(root)
    assert rebuilt.report_digest == report.report_digest
    assert rebuilt.summary == report.summary


def test_context_rebuild_rejects_modified_artifact(tmp_path: Path) -> None:
    root = tmp_path / "context"
    run_context_benchmark(root)
    artifact = next((root / ".run" / "context" / "blobs").glob("*.txt"))
    artifact.write_text(artifact.read_text(encoding="utf-8") + "tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="size mismatch|digest mismatch"):
        rebuild_context_benchmark(root)


def test_context_benchmark_cli_accepts_output_root_and_rebuilds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cli-context"

    assert main(["context", "--output-root", str(root)]) == 0
    output = capsys.readouterr().out
    assert '"tokens_avoided_by_cheap_first"' in output
    assert f"Evidence: {root.resolve()}" in output

    assert main(["context-rebuild", str(root)]) == 0
    assert f"Evidence verified: {root.resolve()}" in capsys.readouterr().out
