from __future__ import annotations

import json

import pytest

from run_agent_evals import (
    RuntimeBenchmarkConfig,
    rebuild_runtime_benchmark,
    run_runtime_benchmarks,
)


@pytest.mark.anyio
async def test_runtime_benchmark_freezes_and_rebuilds_evidence(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "runtime"
    report = await run_runtime_benchmarks(
        root,
        RuntimeBenchmarkConfig(
            scheduler_requests=60,
            scheduler_sessions=6,
            foreground_limit=4,
            background_limit=2,
            tool_calls=4,
            tool_repeats=2,
            tool_delay_ms=1,
            trace_repeats=2,
        ),
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    scheduler = report.summary["scheduler"]
    tools = report.summary["tools"]
    trace = report.summary["trace"]
    assert scheduler["requests"] == 60
    assert scheduler["accepted"] == 60
    assert scheduler["completed"] == 60
    assert scheduler["missing"] == 0
    assert scheduler["duplicates"] == 0
    assert scheduler["session_order_violations"] == 0
    assert tools["order_violations"] == 0
    assert tools["sequential_max_active"] == 1
    assert tools["parallel_max_active"] == 4
    assert trace["requests"] == 2
    assert trace["mean_spans_per_request"] == 9
    assert manifest["repository"]["source_file_count"] > 0
    assert len(manifest["repository"]["source_sha256"]) == 64

    rebuilt = rebuild_runtime_benchmark(root)
    assert rebuilt.report_digest == report.report_digest
    assert rebuilt.summary == report.summary


@pytest.mark.anyio
async def test_runtime_benchmark_rejects_tampered_samples(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "runtime"
    await run_runtime_benchmarks(
        root,
        RuntimeBenchmarkConfig(
            scheduler_requests=12,
            scheduler_sessions=3,
            foreground_limit=2,
            background_limit=1,
            tool_calls=2,
            tool_repeats=1,
            tool_delay_ms=0,
            trace_repeats=1,
        ),
    )
    samples_path = root / "samples.json"
    samples = json.loads(samples_path.read_text(encoding="utf-8"))
    samples["scheduler"]["accepted_count"] = 0
    samples_path.write_text(json.dumps(samples), encoding="utf-8")

    with pytest.raises(ValueError, match="size mismatch|digest mismatch"):
        rebuild_runtime_benchmark(root)
