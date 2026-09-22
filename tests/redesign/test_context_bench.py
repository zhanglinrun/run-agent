from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from time import time

import pytest

from run_agent_coding.context_view import ContextStrategy
from run_agent_core.messages import UserMessage
from run_agent_core.session.entries import CompactionEntry, MessageEntry
from run_agent_core.session.jsonl import entry_to_json_line
from run_agent_evals.cli import main
from run_agent_evals.context_bench import (
    CompactionStrategyExecutor,
    ContextBenchmarkReport,
    rebuild_context_benchmark,
    rebuild_task_context_benchmark,
    run_context_benchmark,
    run_task_context_benchmark,
)
from run_agent_evals.models import ExecutionResult, FrozenTask
from run_agent_evals.runner import TaskExecutor
from run_agent_evals.task_loading import load_tasks


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


def _stub_task_directory(tmp_path: Path) -> tuple[Path, Path]:
    """A frozen two-task manifest over real fixture directories and offline verifiers."""
    manifest_lines = []
    for index, task_id in enumerate(("fix-the-bug", "add-the-feature")):
        fixture = tmp_path / f"fixture-{index}"
        fixture.mkdir(exist_ok=True)
        (fixture / "subject.py").write_text("value = 1\n", encoding="utf-8")
        exit_code = 0 if index == 0 else 1
        manifest_lines.append(
            json.dumps(
                {
                    "id": task_id,
                    "fixture": fixture.name,
                    "prompt": f"complete {task_id}",
                    "verify": [["{python}", "-c", f"raise SystemExit({exit_code})"]],
                    "tags": ["offline"],
                }
            )
        )
    manifest = tmp_path / "tasks.jsonl"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return manifest, tmp_path


class StubTrialExecutor:
    """A deterministic executor: no provider, but real session and telemetry files."""

    def __init__(self, *, summary_calls: int, input_tokens: int, cache_read_tokens: int) -> None:
        self.summary_calls = summary_calls
        self.input_tokens = input_tokens
        self.cache_read_tokens = cache_read_tokens
        self.workspaces: list[Path] = []
        self.settings: list[dict[str, object]] = []

    async def execute(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        self.workspaces.append(workspace)
        settings = json.loads((workspace / ".run" / "settings.json").read_text(encoding="utf-8"))
        self.settings.append(settings)
        session_id = f"stub-{task.id}"
        entries = [
            MessageEntry(message=UserMessage(content=task.prompt)),
            *(
                CompactionEntry(parent_id="previous", summary=f"summary {index}")
                for index in range(self.summary_calls)
            ),
        ]
        # A real transcript makes the summary-call count derivable from the frozen file.
        (workspace / ".run" / "sessions").mkdir(parents=True, exist_ok=True)
        (workspace / ".run" / "sessions" / f"{session_id}.jsonl").write_text(
            "".join(entry_to_json_line(entry) for entry in entries), encoding="utf-8"
        )
        observations = workspace / "observations.jsonl"
        call_stream = f"calls:{task.id}"
        observations.write_text(
            json.dumps(
                {
                    "stream": call_stream,
                    "seq": 1,
                    "created_at": time(),
                    "body": {
                        "type": "provider_call_start",
                        "started_at": time() + 0.02,
                        "logical_call_id": task.id,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return ExecutionResult(
            output=f"finished {task.id}",
            metadata={
                "session_id": session_id,
                "observations": str(observations),
                "call_stream": call_stream,
                "input_tokens": self.input_tokens,
                "output_tokens": 7,
                "cache_read_tokens": self.cache_read_tokens,
                "calls": 2,
                "known_cost": 0.25,
                "cost_complete": True,
            },
        )


async def test_task_context_benchmark_freezes_and_rebuilds_per_task_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, _ = _stub_task_directory(tmp_path)
    root = tmp_path / "task-context"
    executors: dict[str, StubTrialExecutor] = {}

    def factory(strategy: ContextStrategy) -> TaskExecutor:
        executors[strategy] = StubTrialExecutor(
            summary_calls=1 if strategy == "cheap-first" else 2,
            input_tokens=100 if strategy == "cheap-first" else 400,
            cache_read_tokens=10 if strategy == "cheap-first" else 40,
        )
        return CompactionStrategyExecutor(executors[strategy], strategy)

    report = await run_task_context_benchmark(root, load_tasks(manifest), executor_factory=factory)

    assert {path.name for path in root.glob("tasks-*.json")} == {
        "tasks-evidence.json",
        "tasks-inventory.json",
        "tasks-report.json",
    }
    strategies = report.summary["strategies"]
    assert report.summary["task_count"] == 2
    assert report.summary["trials"] == 4
    assert strategies["cheap-first"]["provider_input_tokens"] == 200
    assert strategies["summary-only"]["provider_input_tokens"] == 800
    assert strategies["cheap-first"]["summary_calls"] == 2
    assert strategies["summary-only"]["summary_calls"] == 4
    assert strategies["cheap-first"]["cache_read_tokens"] == 20
    assert strategies["summary-only"]["cache_read_tokens"] == 80
    assert strategies["cheap-first"]["known_cost"] == 0.5
    assert strategies["cheap-first"]["cost_complete"] is True
    # One verifier passes and one fails, so the task result is reported per trial.
    assert strategies["cheap-first"]["passed"] == 1
    assert strategies["cheap-first"]["failed"] == 1
    assert strategies["cheap-first"]["prepare_latency_ms"]["available"] == 2
    assert strategies["cheap-first"]["prepare_latency_ms"]["p50"] > 0
    comparison = report.summary["comparison"]
    assert comparison["provider_input_tokens_saved_by_cheap_first"] == 600
    assert comparison["summary_calls_saved_by_cheap_first"] == 2
    assert comparison["pass_rate_delta"] == 0.0
    per_task = {row["task_id"]: row["strategies"] for row in report.summary["per_task"]}
    assert set(per_task) == {"fix-the-bug", "add-the-feature"}
    for row in per_task.values():
        assert set(row) == {"summary-only", "cheap-first"}
        for arm in row.values():
            assert set(arm) >= {
                "status",
                "provider_input_tokens",
                "summary_calls",
                "prepare_latency_ms",
                "cache_read_tokens",
                "known_cost",
                "verifiers",
            }
            assert arm["summary_calls_source"] == "transcript"

    # Both strategies were pinned into the trial workspace settings.
    assert {json.dumps(item["compaction"]) for item in executors["cheap-first"].settings} == {
        json.dumps({"enabled": True, "strategy": "cheap-first"})
    }
    assert {json.dumps(item["compaction"]) for item in executors["summary-only"].settings} == {
        json.dumps({"enabled": True, "strategy": "summary-only"})
    }

    rebuilt = rebuild_task_context_benchmark(root)
    assert rebuilt.report_digest == report.report_digest
    assert rebuilt.summary == report.summary
    assert rebuilt.evidence_digest == report.evidence_digest

    evidence = json.loads((root / "tasks-evidence.json").read_text(encoding="utf-8"))
    assert evidence["methodology"]["bench_provider_requests"] == 0
    assert evidence["methodology"]["trials"] == 4
    assert {row["id"] for row in evidence["tasks"]} == {"fix-the-bug", "add-the-feature"}

    # A task-only root still rebuilds offline through the CLI.
    assert main(["context-rebuild", str(root)]) == 0
    assert f"Evidence verified: {root.resolve()}" in capsys.readouterr().out


async def test_task_context_rebuild_rejects_a_modified_transcript(tmp_path: Path) -> None:
    manifest, _ = _stub_task_directory(tmp_path)
    root = tmp_path / "task-context"

    def factory(strategy: ContextStrategy) -> TaskExecutor:
        return CompactionStrategyExecutor(
            StubTrialExecutor(summary_calls=1, input_tokens=10, cache_read_tokens=1), strategy
        )

    await run_task_context_benchmark(root, load_tasks(manifest), executor_factory=factory)
    transcript = next(
        path for path in (root / "runtime").rglob("*.jsonl") if path.parent.name == "sessions"
    )
    transcript.write_text(transcript.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="size mismatch|digest mismatch"):
        rebuild_task_context_benchmark(root)


def test_cli_routes_tasks_to_the_task_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run bench context --tasks` must hand the manifest and a per-strategy factory over."""
    manifest, _ = _stub_task_directory(tmp_path)
    root = tmp_path / "cli-task-context"
    seen: dict[str, object] = {}

    async def fake_run_task_context_benchmark(
        output_root: Path,
        tasks: Sequence[FrozenTask],
        *,
        executor_factory: object,
    ) -> ContextBenchmarkReport:
        seen["root"] = output_root
        seen["tasks"] = [task.id for task in tasks]
        seen["factory"] = executor_factory
        return ContextBenchmarkReport(output_root, "e", "i", "r", {"task_count": len(tasks)})

    monkeypatch.setattr(
        "run_agent_evals.cli.run_task_context_benchmark", fake_run_task_context_benchmark
    )

    assert (
        main(
            [
                "context",
                str(root),
                "--tasks",
                str(manifest),
                "--state-root",
                str(tmp_path / "state"),
            ]
        )
        == 0
    )

    assert seen["tasks"] == ["fix-the-bug", "add-the-feature"]
    factory = seen["factory"]
    assert callable(factory)
    executor = factory("cheap-first")  # type: ignore[operator]
    assert isinstance(executor, CompactionStrategyExecutor)
    assert executor.strategy == "cheap-first"
    assert '"task_count": 2' in capsys.readouterr().out
    # The synthetic report is unchanged and rebuild still verifies both artifact sets.
    assert main(["context-rebuild", str(root)]) == 0
    assert f"Evidence verified: {root.resolve()}" in capsys.readouterr().out
