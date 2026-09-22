"""Offline evidence-backed benchmark for protocol-aware context views."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from run_agent_coding.context_view import ContextStrategy, ContextViewPipeline, PreparedContext
from run_agent_core.messages import (
    AgentMessage,
    AssistantContent,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider import ModelRequest
from run_agent_core.session.entries import CompactionEntry
from run_agent_core.session.jsonl import entry_from_json_line
from run_agent_evals.evidence import repository_evidence
from run_agent_evals.models import ExecutionResult, FrozenTask, TrialArtifact
from run_agent_evals.runner import EvaluationRunner, TaskExecutor

CONTEXT_EVIDENCE_SCHEMA = "run-agent.context-benchmark.evidence.v1"
CONTEXT_INVENTORY_SCHEMA = "run-agent.context-benchmark.inventory.v1"
CONTEXT_REPORT_SCHEMA = "run-agent.context-benchmark.report.v1"
CONTEXT_TASK_EVIDENCE_SCHEMA = "run-agent.context-benchmark.tasks.evidence.v1"
CONTEXT_TASK_INVENTORY_SCHEMA = "run-agent.context-benchmark.tasks.inventory.v1"
CONTEXT_TASK_REPORT_SCHEMA = "run-agent.context-benchmark.tasks.report.v1"
_STRATEGIES: tuple[ContextStrategy, ...] = ("summary-only", "cheap-first")


@dataclass(frozen=True, slots=True)
class ContextBenchmarkConfig:
    """Fixed context limits and repetitions used by the local benchmark."""

    context_window_tokens: int = 4_096
    reserve_tokens: int = 512
    spill_chars: int = 6_000
    spill_preview_chars: int = 512
    keep_recent_tokens: int = 1_200
    compact_result_chars: int = 160
    keep_recent_results: int = 2
    prepare_repeats: int = 5

    def __post_init__(self) -> None:
        if self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be at least 1")
        if self.reserve_tokens < 0:
            raise ValueError("reserve_tokens cannot be negative")
        if self.prepare_repeats < 1:
            raise ValueError("prepare_repeats must be at least 1")


@dataclass(frozen=True, slots=True)
class ContextBenchmarkReport:
    root: Path
    evidence_digest: str
    inventory_digest: str
    report_digest: str
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _FrozenSample:
    sample_id: str
    description: str
    profile: Mapping[str, int]
    system: str
    messages: tuple[AgentMessage, ...]

    def request(self) -> ModelRequest:
        return ModelRequest(
            model="context-benchmark",
            system=self.system,
            messages=self.messages,
            tools=(),
            session_id=f"context-benchmark:{self.sample_id}",
        )


def run_context_benchmark(
    root: str | Path,
    config: ContextBenchmarkConfig | None = None,
) -> ContextBenchmarkReport:
    """Measure both context strategies without making any model request."""
    benchmark_config = config or ContextBenchmarkConfig()
    benchmark_root = Path(root).resolve()
    benchmark_root.mkdir(parents=True, exist_ok=True)

    rows = [
        _measure_sample(benchmark_root, benchmark_config, sample) for sample in _frozen_samples()
    ]
    evidence: dict[str, Any] = {
        "schema": CONTEXT_EVIDENCE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": repository_evidence(),
        "config": asdict(benchmark_config),
        "methodology": {
            "provider_requests": 0,
            "strategies": list(_STRATEGIES),
            "latency_clock": "perf_counter_ns",
            "fixtures": "source-defined deterministic provider-neutral transcripts",
        },
        "samples": rows,
    }
    evidence["evidence_digest"] = _canonical_digest(evidence)
    evidence_path = benchmark_root / "evidence.json"
    _freeze_json(evidence_path, evidence, label="context evidence")

    artifact_paths = tuple(
        benchmark_root / relative for relative in sorted(_expected_artifact_paths(evidence))
    )
    inventory = _inventory_payload(benchmark_root, (evidence_path, *artifact_paths))
    _freeze_json(benchmark_root / "inventory.json", inventory, label="context inventory")

    summary = _summarize_evidence(evidence)
    report = _report_payload(evidence, inventory, summary)
    _freeze_json(benchmark_root / "report.json", report, label="context report")
    return ContextBenchmarkReport(
        root=benchmark_root,
        evidence_digest=str(evidence["evidence_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(report["report_digest"]),
        summary=summary,
    )


def rebuild_context_benchmark(root: str | Path) -> ContextBenchmarkReport:
    """Verify frozen context evidence and rebuild its report entirely offline."""
    benchmark_root = Path(root).resolve()
    evidence = _read_object(benchmark_root / "evidence.json")
    _verify_embedded_digest(evidence, "evidence_digest", CONTEXT_EVIDENCE_SCHEMA)
    inventory = _read_object(benchmark_root / "inventory.json")
    _verify_embedded_digest(inventory, "inventory_digest", CONTEXT_INVENTORY_SCHEMA)
    _verify_inventory(benchmark_root, inventory, evidence)

    summary = _summarize_evidence(evidence)
    expected = _report_payload(evidence, inventory, summary)
    stored = _read_object(benchmark_root / "report.json")
    _verify_embedded_digest(stored, "report_digest", CONTEXT_REPORT_SCHEMA)
    if _canonical_digest(stored) != _canonical_digest(expected):
        raise ValueError("context report does not match the frozen evidence")
    return ContextBenchmarkReport(
        root=benchmark_root,
        evidence_digest=str(evidence["evidence_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(stored["report_digest"]),
        summary=summary,
    )


@dataclass(slots=True)
class CompactionStrategyExecutor:
    """Pin one compaction strategy for a trial, then delegate to the real executor.

    A coding session reads ``compaction.strategy`` from its project settings file, so the
    two arms of the task benchmark differ only by the settings file this executor writes
    into the trial workspace before delegating.
    """

    inner: TaskExecutor
    strategy: ContextStrategy

    async def execute(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        settings = workspace / ".run" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"compaction": {"enabled": True, "strategy": self.strategy}}) + "\n",
            encoding="utf-8",
        )
        return await self.inner.execute(task, workspace)


TaskExecutorFactory = Callable[[ContextStrategy], TaskExecutor]


async def run_task_context_benchmark(
    root: str | Path,
    tasks: Sequence[FrozenTask],
    *,
    executor_factory: TaskExecutorFactory,
    seeds: tuple[int, ...] = (0,),
) -> ContextBenchmarkReport:
    """Freeze a cheap-first vs summary-only comparison over real coding tasks.

    Every task is run once per strategy through the caller's executor, so the benchmark
    itself never contacts a provider. Per trial it reports provider input tokens, cache
    read tokens and known cost from the executor's accounting metadata, the L4 summary
    call count as persisted ``CompactionEntry`` rows in the produced session transcript,
    preparation latency from the trial start to its first recorded provider call, and the
    task result with its verifier exit codes.
    """
    benchmark_root = Path(root).resolve()
    benchmark_root.mkdir(parents=True, exist_ok=True)
    ordered = _validated_tasks(tasks)
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("task benchmark seeds must be non-empty and unique")
    runtime_root = benchmark_root / "runtime"
    trials: list[dict[str, Any]] = []
    for strategy in _STRATEGIES:
        executor = _CapturingExecutor(executor_factory(strategy))
        runner = EvaluationRunner(runtime_root / strategy, keep_workspaces=True)
        for seed in seeds:
            for task in ordered:
                trials.append(
                    await _run_task_trial(benchmark_root, runner, executor, task, strategy, seed)
                )
    evidence: dict[str, Any] = {
        "schema": CONTEXT_TASK_EVIDENCE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": repository_evidence(),
        "config": {"seeds": list(seeds), "keep_workspaces": True},
        "methodology": {
            # The benchmark itself contacts nothing; each trial brings its own accounting.
            "bench_provider_requests": 0,
            "trials": len(trials),
            "strategies": list(_STRATEGIES),
            "latency_clock": "perf_counter_ns",
            "prepare_latency": "trial start to its first recorded provider call start",
            "summary_calls": "persisted CompactionEntry rows in the trial session transcript",
            "accounting": (
                "ExecutionResult.metadata: input_tokens, cache_read_tokens, calls, known_cost"
            ),
            "tasks_source": "frozen JSONL task manifest",
        },
        "tasks": [_task_evidence(task) for task in ordered],
        "trials": trials,
    }
    evidence["evidence_digest"] = _canonical_digest(evidence)
    evidence_path = benchmark_root / "tasks-evidence.json"
    _freeze_json(evidence_path, evidence, label="task context evidence")

    artifact_paths = tuple(
        benchmark_root / relative for relative in sorted(_expected_task_artifact_paths(evidence))
    )
    inventory = _task_inventory_payload(benchmark_root, (evidence_path, *artifact_paths))
    _freeze_json(benchmark_root / "tasks-inventory.json", inventory, label="task context inventory")
    summary = _summarize_task_evidence(evidence)
    report = _task_report_payload(evidence, inventory, summary)
    _freeze_json(benchmark_root / "tasks-report.json", report, label="task context report")
    return ContextBenchmarkReport(
        root=benchmark_root,
        evidence_digest=str(evidence["evidence_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(report["report_digest"]),
        summary=summary,
    )


def rebuild_task_context_benchmark(root: str | Path) -> ContextBenchmarkReport:
    """Verify frozen task evidence and rebuild its report entirely offline."""
    benchmark_root = Path(root).resolve()
    evidence = _read_object(benchmark_root / "tasks-evidence.json")
    _verify_embedded_digest(evidence, "evidence_digest", CONTEXT_TASK_EVIDENCE_SCHEMA)
    inventory = _read_object(benchmark_root / "tasks-inventory.json")
    _verify_embedded_digest(inventory, "inventory_digest", CONTEXT_TASK_INVENTORY_SCHEMA)
    _verify_task_inventory(benchmark_root, inventory, evidence)

    summary = _summarize_task_evidence(evidence)
    expected = _task_report_payload(evidence, inventory, summary)
    stored = _read_object(benchmark_root / "tasks-report.json")
    _verify_embedded_digest(stored, "report_digest", CONTEXT_TASK_REPORT_SCHEMA)
    if _canonical_digest(stored) != _canonical_digest(expected):
        raise ValueError("task context report does not match the frozen evidence")
    return ContextBenchmarkReport(
        root=benchmark_root,
        evidence_digest=str(evidence["evidence_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(stored["report_digest"]),
        summary=summary,
    )


@dataclass(slots=True)
class _CapturingExecutor:
    """Remember the workspace of the trial currently running."""

    inner: TaskExecutor
    workspace: Path | None = None

    async def execute(self, task: FrozenTask, workspace: Path) -> ExecutionResult:
        self.workspace = workspace
        return await self.inner.execute(task, workspace)


async def _run_task_trial(
    root: Path,
    runner: EvaluationRunner,
    executor: _CapturingExecutor,
    task: FrozenTask,
    strategy: ContextStrategy,
    seed: int,
) -> dict[str, Any]:
    artifact = await runner.run_trial(task, executor, candidate_id=strategy, seed=seed)
    metadata = artifact.metadata
    transcript = _locate_transcript(executor.workspace, metadata)
    summary_calls, summary_source = _summary_call_count(metadata, transcript)
    return {
        "task_id": task.id,
        "strategy": strategy,
        "seed": seed,
        "status": artifact.status,
        "error": artifact.error,
        "provider_input_tokens": _metadata_int(metadata, "input_tokens"),
        "provider_output_tokens": _metadata_int(metadata, "output_tokens"),
        "cache_read_tokens": _metadata_int(metadata, "cache_read_tokens"),
        "logical_calls": _metadata_int(metadata, "calls"),
        "summary_calls": summary_calls,
        "summary_calls_source": summary_source,
        "prepare_latency_ms": _prepare_latency_ms(artifact),
        "trial_duration_ms": artifact.duration_ms,
        "known_cost": _metadata_float(metadata, "known_cost"),
        "cost_complete": metadata.get("cost_complete") is True,
        "workspace_digest": artifact.workspace_digest_before,
        "verifiers": [
            {
                "command": list(result.command),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
            }
            for result in artifact.verifiers
        ],
        "transcript": _transcript_evidence(root, transcript),
    }


def _task_evidence(task: FrozenTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "prompt_digest": _canonical_digest({"prompt": task.prompt}),
        "prompt_chars": len(task.prompt),
        "verify": [list(command) for command in task.verify],
        "tags": list(task.tags),
        "fixture_digest": _directory_digest(task.fixture),
    }


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _locate_transcript(workspace: Path | None, metadata: Mapping[str, Any]) -> Path | None:
    """Find the session transcript a coding executor left inside its trial workspace."""
    session_id = metadata.get("session_id")
    if workspace is None or not isinstance(session_id, str) or not session_id:
        return None
    candidate = workspace / ".run" / "sessions" / f"{session_id}.jsonl"
    return candidate if candidate.is_file() else None


def _transcript_evidence(root: Path, transcript: Path | None) -> dict[str, Any] | None:
    if transcript is None:
        return None
    payload = transcript.read_bytes()
    try:
        relative = transcript.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return {"path": relative, "bytes": len(payload), "sha256": _sha256(payload)}


def _summary_call_count(metadata: Mapping[str, Any], transcript: Path | None) -> tuple[int, str]:
    """Count L4 summary calls: executor metadata first, else persisted compactions."""
    reported = metadata.get("summary_calls")
    if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
        return reported, "metadata"
    if transcript is None:
        return 0, "unavailable"
    return _count_compactions(transcript), "transcript"


def _count_compactions(transcript: Path) -> int:
    count = 0
    for line in transcript.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = entry_from_json_line(line)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, CompactionEntry):
            count += 1
    return count


def _prepare_latency_ms(artifact: TrialArtifact) -> float | None:
    """Time from the trial start to its first recorded provider call, when recorded."""
    metadata = artifact.metadata
    observations = metadata.get("observations")
    stream = metadata.get("call_stream")
    if isinstance(observations, str) and isinstance(stream, str):
        path = Path(observations)
        if path.is_file():
            started = [
                float(body["started_at"])
                for row in _jsonl_objects(path)
                if row.get("stream") == stream
                for body in (row.get("body"),)
                if isinstance(body, Mapping)
                and body.get("type") in {"provider_call_start", "provider_call"}
                and isinstance(body.get("started_at"), int | float)
            ]
            if started:
                return max(0.0, (min(started) - artifact.started_at) * 1000)
    reported = metadata.get("prepare_latency_ms")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        return float(reported)
    return None


def _jsonl_objects(path: Path) -> Iterator[Mapping[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            yield payload


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int:
    value = metadata.get(key)
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _metadata_float(metadata: Mapping[str, Any], key: str) -> float:
    value = metadata.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _measure_sample(
    root: Path,
    config: ContextBenchmarkConfig,
    sample: _FrozenSample,
) -> dict[str, Any]:
    fixture = {
        "system": sample.system,
        "messages": [
            message.model_dump(mode="json", by_alias=False) for message in sample.messages
        ],
    }
    strategy_rows: dict[str, Any] = {}
    for strategy in _STRATEGIES:
        pipeline = ContextViewPipeline(
            cwd=root,
            context_window_tokens=config.context_window_tokens,
            reserve_tokens=config.reserve_tokens,
            strategy=strategy,
            spill_chars=config.spill_chars,
            spill_preview_chars=config.spill_preview_chars,
            keep_recent_tokens=config.keep_recent_tokens,
            compact_result_chars=config.compact_result_chars,
            keep_recent_results=config.keep_recent_results,
        )
        durations: list[float] = []
        prepared: PreparedContext | None = None
        signature: tuple[object, ...] | None = None
        for _ in range(config.prepare_repeats):
            started = perf_counter_ns()
            current = pipeline.prepare(sample.request())
            durations.append((perf_counter_ns() - started) / 1_000_000)
            current_signature = _prepared_signature(current)
            if signature is not None and current_signature != signature:
                raise RuntimeError(
                    f"context preparation was not deterministic for {sample.sample_id}:{strategy}"
                )
            prepared = current
            signature = current_signature
        if prepared is None:
            raise RuntimeError("context benchmark produced no preparation result")
        strategy_rows[strategy] = {
            "tokens_before": prepared.tokens_before,
            "tokens_after": prepared.tokens_after,
            "layers": list(prepared.layers),
            "artifact_count": len(prepared.artifacts),
            "artifacts": [asdict(artifact) for artifact in prepared.artifacts],
            "prepare_latency_ms": durations,
            "protocol_pairing_valid": _protocol_pairing_valid(prepared.request.messages),
            "summary_needed": prepared.needs_l4,
        }
    return {
        "sample_id": sample.sample_id,
        "description": sample.description,
        "profile": dict(sample.profile),
        "fixture_digest": _canonical_digest(fixture),
        "strategies": strategy_rows,
    }


def _prepared_signature(prepared: PreparedContext) -> tuple[object, ...]:
    messages = [
        message.model_dump(mode="json", by_alias=False, exclude={"timestamp"})
        for message in prepared.request.messages
    ]
    return (
        prepared.tokens_before,
        prepared.tokens_after,
        prepared.layers,
        prepared.artifacts,
        prepared.stable_prefix_digest,
        prepared.needs_l4,
        _canonical_digest({"messages": messages}),
    )


def _protocol_pairing_valid(messages: Sequence[AgentMessage]) -> bool:
    """Check that every tool call is followed by its ordered result and no result is orphaned."""
    seen_call_ids: set[str] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, ToolResultMessage):
            return False
        if not isinstance(message, AssistantMessage) or not message.tool_calls:
            index += 1
            continue
        calls = message.tool_calls
        call_ids = [call.id for call in calls]
        if len(set(call_ids)) != len(call_ids) or seen_call_ids.intersection(call_ids):
            return False
        following = messages[index + 1 : index + 1 + len(calls)]
        if len(following) != len(calls):
            return False
        for call, result in zip(calls, following, strict=True):
            if not isinstance(result, ToolResultMessage):
                return False
            if result.tool_call_id != call.id or result.tool_name != call.name:
                return False
        seen_call_ids.update(call_ids)
        index += len(calls) + 1
    return True


def _summarize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    config = _mapping(evidence, "config")
    repeats = int(config.get("prepare_repeats", 0))
    if repeats < 1:
        raise ValueError("context evidence prepare_repeats must be at least 1")
    samples = _sequence_of_mappings(evidence, "samples")
    if not samples:
        raise ValueError("context evidence contains no samples")
    sample_ids = [str(sample.get("sample_id", "")) for sample in samples]
    if any(not sample_id for sample_id in sample_ids) or len(set(sample_ids)) != len(sample_ids):
        raise ValueError("context evidence sample ids must be non-empty and unique")

    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy in _STRATEGIES:
        tokens_before = 0
        tokens_after = 0
        artifact_count = 0
        summary_needed_count = 0
        pairing_valid_count = 0
        latencies: list[float] = []
        layer_counts: dict[str, int] = {}
        for sample in samples:
            fixture_digest = sample.get("fixture_digest")
            if not isinstance(fixture_digest, str) or len(fixture_digest) != 64:
                raise ValueError("context evidence fixture_digest must be a SHA-256 string")
            strategies = _mapping(sample, "strategies")
            if set(strategies) != set(_STRATEGIES):
                raise ValueError("context evidence must contain both context strategies")
            result_value = strategies.get(strategy)
            if not isinstance(result_value, Mapping):
                raise ValueError(f"context strategy {strategy!r} must be an object")
            result = result_value
            before = _non_negative_int(result, "tokens_before")
            after = _non_negative_int(result, "tokens_after")
            artifacts = _sequence_of_mappings(result, "artifacts")
            count = _non_negative_int(result, "artifact_count")
            if count != len(artifacts):
                raise ValueError("context artifact_count does not match artifact evidence")
            latency_values = _sequence(result, "prepare_latency_ms")
            if len(latency_values) != repeats:
                raise ValueError("context prepare latency sample count does not match config")
            for value in latency_values:
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    raise ValueError("context prepare latency must contain non-negative numbers")
                latencies.append(float(value))
            layers = _sequence(result, "layers")
            if any(not isinstance(layer, str) for layer in layers):
                raise ValueError("context layers must contain strings")
            for layer in layers:
                layer_name = str(layer)
                layer_counts[layer_name] = layer_counts.get(layer_name, 0) + 1
            pairing = result.get("protocol_pairing_valid")
            summary_needed = result.get("summary_needed")
            if not isinstance(pairing, bool) or not isinstance(summary_needed, bool):
                raise ValueError("context validity and summary-needed fields must be booleans")
            tokens_before += before
            tokens_after += after
            artifact_count += count
            pairing_valid_count += int(pairing)
            summary_needed_count += int(summary_needed)
        by_strategy[strategy] = {
            "sample_count": len(samples),
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_removed": tokens_before - tokens_after,
            "token_reduction_ratio": (
                (tokens_before - tokens_after) / tokens_before if tokens_before else 0.0
            ),
            "layer_counts": dict(sorted(layer_counts.items())),
            "artifact_count": artifact_count,
            "prepare_latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
            "protocol_pairing_valid_count": pairing_valid_count,
            "protocol_pairing_all_valid": pairing_valid_count == len(samples),
            "summary_needed_count": summary_needed_count,
        }

    for sample in samples:
        strategies = _mapping(sample, "strategies")
        summary_only = _mapping(strategies, "summary-only")
        cheap_first = _mapping(strategies, "cheap-first")
        if summary_only.get("tokens_before") != cheap_first.get("tokens_before"):
            raise ValueError("context strategies disagree on tokens_before")

    summary_only_row = by_strategy["summary-only"]
    cheap_first_row = by_strategy["cheap-first"]
    summary_latency = _mapping(summary_only_row, "prepare_latency_ms")
    cheap_latency = _mapping(cheap_first_row, "prepare_latency_ms")
    return {
        "sample_count": len(samples),
        "strategies": by_strategy,
        "comparison": {
            "tokens_avoided_by_cheap_first": int(summary_only_row["tokens_after"])
            - int(cheap_first_row["tokens_after"]),
            "summary_needed_avoided_by_cheap_first": int(summary_only_row["summary_needed_count"])
            - int(cheap_first_row["summary_needed_count"]),
            "cheap_first_prepare_latency_p50_delta_ms": float(cheap_latency["p50"])
            - float(summary_latency["p50"]),
            "cheap_first_protocol_pairing_regressions": max(
                0,
                int(summary_only_row["protocol_pairing_valid_count"])
                - int(cheap_first_row["protocol_pairing_valid_count"]),
            ),
        },
    }


def _frozen_samples() -> tuple[_FrozenSample, ...]:
    return (_large_result_sample(), _parallel_calls_sample(), _multi_turn_sample())


def _large_result_sample() -> _FrozenSample:
    call = ToolCall(id="large-read-0", name="read", arguments={"path": "build/output.log"})
    return _FrozenSample(
        sample_id="large-tool-result",
        description="One oversized tool result exercises content-addressed L3 spilling.",
        profile={
            "turns": 1,
            "tool_calls": 1,
            "tool_results": 1,
            "long_tool_results": 1,
            "parallel_tool_groups": 0,
        },
        system="You are an offline context benchmark.",
        messages=(
            UserMessage(content="Inspect the complete build log.", timestamp=1_000),
            AssistantMessage(
                content=list[AssistantContent]([call]),
                model="context-benchmark",
                stop_reason="toolUse",
                timestamp=1_001,
            ),
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text=_payload("build-log", 28_000))],
                timestamp=1_002,
            ),
            AssistantMessage(
                content=[TextContent(text="The build log was inspected.")],
                model="context-benchmark",
                timestamp=1_003,
            ),
        ),
    )


def _parallel_calls_sample() -> _FrozenSample:
    calls = tuple(
        ToolCall(id=f"parallel-read-{index}", name="read", arguments={"path": f"src/{index}.py"})
        for index in range(6)
    )
    results = tuple(
        ToolResultMessage(
            tool_call_id=call.id,
            tool_name=call.name,
            content=[TextContent(text=_payload(f"source-{index}", 3_200))],
            timestamp=2_002 + index,
        )
        for index, call in enumerate(calls)
    )
    return _FrozenSample(
        sample_id="parallel-tool-calls",
        description="Six paired parallel calls exercise old-result L2 compaction.",
        profile={
            "turns": 1,
            "tool_calls": len(calls),
            "tool_results": len(results),
            "long_tool_results": 0,
            "parallel_tool_groups": 1,
        },
        system="You are an offline context benchmark.",
        messages=(
            UserMessage(content="Read all six source files in parallel.", timestamp=2_000),
            AssistantMessage(
                content=list[AssistantContent](calls),
                model="context-benchmark",
                stop_reason="toolUse",
                timestamp=2_001,
            ),
            *results,
            AssistantMessage(
                content=[TextContent(text="All source files were compared.")],
                model="context-benchmark",
                timestamp=2_008,
            ),
        ),
    )


def _multi_turn_sample() -> _FrozenSample:
    messages: list[AgentMessage] = [
        UserMessage(content="Refactor the parser while preserving behavior.", timestamp=3_000)
    ]
    timestamp = 3_001
    for index in range(5):
        call = ToolCall(
            id=f"turn-read-{index}",
            name="read",
            arguments={"path": f"parser/phase_{index}.py"},
        )
        messages.extend(
            (
                UserMessage(content=f"Continue with parser phase {index}.", timestamp=timestamp),
                AssistantMessage(
                    content=list[AssistantContent]([call]),
                    model="context-benchmark",
                    stop_reason="toolUse",
                    timestamp=timestamp + 1,
                ),
                ToolResultMessage(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=[TextContent(text=_payload(f"phase-{index}", 4_000))],
                    timestamp=timestamp + 2,
                ),
                AssistantMessage(
                    content=[TextContent(text=f"Parser phase {index} is complete.")],
                    model="context-benchmark",
                    timestamp=timestamp + 3,
                ),
            )
        )
        timestamp += 4
    return _FrozenSample(
        sample_id="multi-turn-history",
        description="Five completed turns exercise protocol-aware L1 middle folding.",
        profile={
            "turns": 5,
            "tool_calls": 5,
            "tool_results": 5,
            "long_tool_results": 0,
            "parallel_tool_groups": 0,
        },
        system="You are an offline context benchmark.",
        messages=tuple(messages),
    )


def _payload(label: str, length: int) -> str:
    unit = f"{label}:0123456789abcdef\n"
    return (unit * ((length + len(unit) - 1) // len(unit)))[:length]


def _inventory_payload(root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CONTEXT_INVENTORY_SCHEMA,
        "files": {
            path.relative_to(root).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path.read_bytes()),
            }
            for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
        },
    }
    payload["inventory_digest"] = _canonical_digest(payload)
    return payload


def _report_payload(
    evidence: Mapping[str, Any],
    inventory: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CONTEXT_REPORT_SCHEMA,
        "evidence_digest": evidence["evidence_digest"],
        "inventory_digest": inventory["inventory_digest"],
        "summary": summary,
    }
    payload["report_digest"] = _canonical_digest(payload)
    return payload


def _verify_inventory(
    root: Path,
    inventory: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    files = _mapping(inventory, "files")
    artifact_digests = _expected_artifact_paths(evidence)
    expected_paths = {"evidence.json", *artifact_digests}
    if set(files) != expected_paths:
        raise ValueError("context inventory does not match the frozen evidence files")
    for relative, receipt_value in files.items():
        if not isinstance(relative, str) or not isinstance(receipt_value, Mapping):
            raise ValueError("invalid context inventory receipt")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"context evidence file is missing or outside its root: {relative}")
        if path.stat().st_size != int(receipt_value.get("bytes", -1)):
            raise ValueError(f"context evidence size mismatch: {relative}")
        digest = _sha256(path.read_bytes())
        if digest != receipt_value.get("sha256"):
            raise ValueError(f"context evidence digest mismatch: {relative}")
        expected_digest = artifact_digests.get(relative)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError(f"context artifact digest mismatch: {relative}")


def _expected_artifact_paths(evidence: Mapping[str, Any]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for sample in _sequence_of_mappings(evidence, "samples"):
        strategies = _mapping(sample, "strategies")
        for strategy in _STRATEGIES:
            result = _mapping(strategies, strategy)
            for artifact in _sequence_of_mappings(result, "artifacts"):
                relative = artifact.get("relative_path")
                digest = artifact.get("digest")
                if not isinstance(relative, str) or not isinstance(digest, str):
                    raise ValueError("context artifact path and digest must be strings")
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise ValueError(f"unsafe context artifact path: {relative}")
                previous = expected.setdefault(relative_path.as_posix(), digest)
                if previous != digest:
                    raise ValueError(f"conflicting context artifact digest: {relative}")
    return expected


def _validated_tasks(tasks: Sequence[FrozenTask]) -> tuple[FrozenTask, ...]:
    if not tasks:
        raise ValueError("task context benchmark requires at least one task")
    ids = [task.id for task in tasks]
    if any(not task_id for task_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("task context benchmark task ids must be non-empty and unique")
    return tuple(tasks)


def _expected_task_artifact_paths(evidence: Mapping[str, Any]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for trial in _sequence_of_mappings(evidence, "trials"):
        transcript = trial.get("transcript")
        if not isinstance(transcript, Mapping):
            continue
        relative = transcript.get("path")
        digest = transcript.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("task transcript evidence needs a path and sha256")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe task transcript path: {relative}")
        previous = expected.setdefault(relative_path.as_posix(), digest)
        if previous != digest:
            raise ValueError(f"conflicting task transcript digest: {relative}")
    return expected


def _task_inventory_payload(root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CONTEXT_TASK_INVENTORY_SCHEMA,
        "files": {
            path.relative_to(root).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path.read_bytes()),
            }
            for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
        },
    }
    payload["inventory_digest"] = _canonical_digest(payload)
    return payload


def _task_report_payload(
    evidence: Mapping[str, Any],
    inventory: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": CONTEXT_TASK_REPORT_SCHEMA,
        "evidence_digest": evidence["evidence_digest"],
        "inventory_digest": inventory["inventory_digest"],
        "summary": summary,
    }
    payload["report_digest"] = _canonical_digest(payload)
    return payload


def _verify_task_inventory(
    root: Path,
    inventory: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    files = _mapping(inventory, "files")
    transcripts = _expected_task_artifact_paths(evidence)
    if set(files) != {"tasks-evidence.json", *transcripts}:
        raise ValueError("task context inventory does not match the frozen evidence files")
    for relative, receipt_value in files.items():
        if not isinstance(relative, str) or not isinstance(receipt_value, Mapping):
            raise ValueError("invalid task context inventory receipt")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"task context evidence is missing or outside its root: {relative}")
        if path.stat().st_size != int(receipt_value.get("bytes", -1)):
            raise ValueError(f"task context evidence size mismatch: {relative}")
        digest = _sha256(path.read_bytes())
        if digest != receipt_value.get("sha256"):
            raise ValueError(f"task context evidence digest mismatch: {relative}")
        expected_digest = transcripts.get(relative)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError(f"task transcript digest mismatch: {relative}")


def _summarize_task_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    trials = _sequence_of_mappings(evidence, "trials")
    if not trials:
        raise ValueError("task context evidence contains no trials")
    tasks = _sequence_of_mappings(evidence, "tasks")
    task_ids = [str(task.get("id", "")) for task in tasks]
    if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("task context evidence task ids must be non-empty and unique")

    by_strategy = {strategy: _summarize_task_strategy(trials, strategy) for strategy in _STRATEGIES}
    observed = {(str(trial.get("task_id")), str(trial.get("strategy"))) for trial in trials}
    expected = {(task_id, strategy) for task_id in task_ids for strategy in _STRATEGIES}
    if observed != expected:
        raise ValueError("task context evidence must carry every task and strategy pair")

    summary_only, cheap_first = by_strategy["summary-only"], by_strategy["cheap-first"]
    return {
        "task_count": len(task_ids),
        "trials": len(trials),
        "strategies": by_strategy,
        "comparison": {
            "provider_input_tokens_saved_by_cheap_first": int(summary_only["provider_input_tokens"])
            - int(cheap_first["provider_input_tokens"]),
            "summary_calls_saved_by_cheap_first": int(summary_only["summary_calls"])
            - int(cheap_first["summary_calls"]),
            "cache_read_tokens_delta": int(cheap_first["cache_read_tokens"])
            - int(summary_only["cache_read_tokens"]),
            "known_cost_delta": float(cheap_first["known_cost"])
            - float(summary_only["known_cost"]),
            "prepare_latency_p50_delta_ms": _latency_delta(summary_only, cheap_first),
            "pass_rate_delta": float(cheap_first["pass_rate"]) - float(summary_only["pass_rate"]),
        },
        "per_task": _task_rows(trials),
    }


def _summarize_task_strategy(
    trials: Sequence[Mapping[str, Any]], strategy: ContextStrategy
) -> dict[str, Any]:
    rows = [trial for trial in trials if trial.get("strategy") == strategy]
    if not rows:
        raise ValueError(f"task context evidence contains no {strategy} trials")
    statuses = [str(row.get("status")) for row in rows]
    latencies = [
        float(row["prepare_latency_ms"])
        for row in rows
        if isinstance(row.get("prepare_latency_ms"), int | float)
        and not isinstance(row.get("prepare_latency_ms"), bool)
    ]
    durations = [
        float(row["trial_duration_ms"])
        for row in rows
        if isinstance(row.get("trial_duration_ms"), int | float)
    ]
    priced = [row for row in rows if row.get("cost_complete") is True]
    return {
        "trials": len(rows),
        "passed": statuses.count("passed"),
        "failed": statuses.count("failed"),
        "errored": sum(status not in {"passed", "failed"} for status in statuses),
        "pass_rate": statuses.count("passed") / len(rows),
        "provider_input_tokens": sum(
            _non_negative_int(row, "provider_input_tokens") for row in rows
        ),
        "provider_output_tokens": sum(
            _non_negative_int(row, "provider_output_tokens") for row in rows
        ),
        "cache_read_tokens": sum(_non_negative_int(row, "cache_read_tokens") for row in rows),
        "logical_calls": sum(_non_negative_int(row, "logical_calls") for row in rows),
        "summary_calls": sum(_non_negative_int(row, "summary_calls") for row in rows),
        "known_cost": sum(float(row.get("known_cost") or 0.0) for row in rows),
        "priced_trials": len(priced),
        "cost_complete": len(priced) == len(rows),
        "prepare_latency_ms": {
            "available": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
        "trial_duration_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
    }


def _latency_delta(summary_only: Mapping[str, Any], cheap_first: Mapping[str, Any]) -> float | None:
    left = _mapping(summary_only, "prepare_latency_ms")
    right = _mapping(cheap_first, "prepare_latency_ms")
    if not left.get("available") or not right.get("available"):
        return None
    return float(right["p50"]) - float(left["p50"])


def _task_rows(trials: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Raw per-task comparison rows, one per task with both strategies side by side."""
    by_task: dict[str, dict[str, dict[str, Any]]] = {}
    for trial in trials:
        task_id = str(trial.get("task_id"))
        strategy = str(trial.get("strategy"))
        by_task.setdefault(task_id, {})[strategy] = {
            "status": trial.get("status"),
            "provider_input_tokens": _non_negative_int(trial, "provider_input_tokens"),
            "cache_read_tokens": _non_negative_int(trial, "cache_read_tokens"),
            "summary_calls": _non_negative_int(trial, "summary_calls"),
            "summary_calls_source": trial.get("summary_calls_source"),
            "prepare_latency_ms": trial.get("prepare_latency_ms"),
            "trial_duration_ms": trial.get("trial_duration_ms"),
            "known_cost": float(trial.get("known_cost") or 0.0),
            "cost_complete": trial.get("cost_complete") is True,
            "verifiers": [
                {"command": list(item.get("command", [])), "exit_code": item.get("exit_code")}
                for item in _sequence_of_mappings(trial, "verifiers")
            ],
        }
    return [{"task_id": task_id, "strategies": by_task[task_id]} for task_id in sorted(by_task)]


def _verify_embedded_digest(
    payload: Mapping[str, Any],
    digest_key: str,
    schema: str,
) -> None:
    if payload.get("schema") != schema:
        raise ValueError(f"unsupported context evidence schema: {payload.get('schema')!r}")
    if payload.get(digest_key) != _digest_without(payload, digest_key):
        raise ValueError(f"context evidence {digest_key} does not match its content")


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"context evidence field {key!r} must be an object")
    return value


def _sequence(payload: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"context evidence field {key!r} must be an array")
    return value


def _sequence_of_mappings(
    payload: Mapping[str, Any],
    key: str,
) -> list[Mapping[str, Any]]:
    values = _sequence(payload, key)
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError(f"context evidence field {key!r} must contain objects")
    return [value for value in values if isinstance(value, Mapping)]


def _non_negative_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"context evidence field {key!r} must be a non-negative integer")
    return value


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _freeze_json(path: Path, payload: Mapping[str, Any], *, label: str) -> None:
    if path.exists():
        if _canonical_digest(_read_object(path)) != _canonical_digest(payload):
            raise ValueError(f"existing {label} does not match this benchmark")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _digest_without(payload: Mapping[str, Any], key: str) -> str:
    return _canonical_digest({name: value for name, value in payload.items() if name != key})


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CONTEXT_EVIDENCE_SCHEMA",
    "CONTEXT_INVENTORY_SCHEMA",
    "CONTEXT_REPORT_SCHEMA",
    "CONTEXT_TASK_EVIDENCE_SCHEMA",
    "CONTEXT_TASK_INVENTORY_SCHEMA",
    "CONTEXT_TASK_REPORT_SCHEMA",
    "CompactionStrategyExecutor",
    "ContextBenchmarkConfig",
    "ContextBenchmarkReport",
    "TaskExecutorFactory",
    "rebuild_context_benchmark",
    "rebuild_task_context_benchmark",
    "run_context_benchmark",
    "run_task_context_benchmark",
]
