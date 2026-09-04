"""Evidence-backed deterministic benchmarks for scheduler, tools, and tracing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from run_agent_ai.fake import FakeProvider
from run_agent_core.events import AgentEvent, MessageEndEvent
from run_agent_core.loop import run_agent_loop
from run_agent_core.messages import (
    AssistantContent,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider_events import (
    AssistantDoneEvent,
    AssistantStartEvent,
    ToolCallEndEvent,
)
from run_agent_core.tools import AgentTool, AgentToolResult
from run_agent_core.types import JSONValue
from run_agent_evals.evidence import repository_evidence
from run_agent_gateway import TurnRequest, TurnResult, TurnScheduler
from run_agent_observability import TraceRecorder, percentile

RUNTIME_MANIFEST_SCHEMA = "run-agent.runtime-benchmark.manifest.v1"
RUNTIME_SAMPLES_SCHEMA = "run-agent.runtime-benchmark.samples.v1"
RUNTIME_INVENTORY_SCHEMA = "run-agent.runtime-benchmark.inventory.v1"
RUNTIME_REPORT_SCHEMA = "run-agent.runtime-benchmark.report.v1"


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkConfig:
    scheduler_requests: int = 10_000
    scheduler_sessions: int = 100
    foreground_limit: int = 32
    background_limit: int = 8
    tool_calls: int = 8
    tool_repeats: int = 9
    tool_delay_ms: float = 20.0
    trace_repeats: int = 9

    def __post_init__(self) -> None:
        positive_ints = {
            "scheduler_requests": self.scheduler_requests,
            "scheduler_sessions": self.scheduler_sessions,
            "foreground_limit": self.foreground_limit,
            "background_limit": self.background_limit,
            "tool_calls": self.tool_calls,
            "tool_repeats": self.tool_repeats,
            "trace_repeats": self.trace_repeats,
        }
        for name, value in positive_ints.items():
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.scheduler_sessions > self.scheduler_requests:
            raise ValueError("scheduler_sessions cannot exceed scheduler_requests")
        if self.tool_delay_ms < 0:
            raise ValueError("tool_delay_ms cannot be negative")


@dataclass(frozen=True, slots=True)
class RuntimeBenchmarkReport:
    root: Path
    manifest_digest: str
    inventory_digest: str
    report_digest: str
    summary: dict[str, Any]


class _SchedulerRunner:
    def __init__(self) -> None:
        self.started_by_session: defaultdict[str, list[int]] = defaultdict(list)
        self.active = 0
        self.max_active = 0

    async def run(self, request: TurnRequest, cancellation: asyncio.Event) -> TurnResult:
        sequence = request.metadata.get("sequence")
        if not isinstance(sequence, int):
            raise ValueError("benchmark request is missing its sequence")
        self.started_by_session[request.session_id].append(sequence)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if cancellation.is_set():
                return TurnResult.cancelled(request)
            return TurnResult.succeeded(request, output=str(sequence))
        finally:
            self.active -= 1


async def run_runtime_benchmarks(
    root: str | Path,
    config: RuntimeBenchmarkConfig | None = None,
) -> RuntimeBenchmarkReport:
    """Run local benchmarks and freeze raw samples plus content receipts."""
    benchmark_config = config or RuntimeBenchmarkConfig()
    benchmark_root = Path(root).resolve()
    benchmark_root.mkdir(parents=True, exist_ok=True)
    manifest = _manifest_payload(benchmark_config)
    _freeze_json(benchmark_root / "manifest.json", manifest, label="runtime manifest")

    scheduler = await _scheduler_samples(benchmark_config)
    tools = await _tool_samples(benchmark_config)
    trace, trace_paths = await _trace_samples(benchmark_root, benchmark_config)
    samples: dict[str, Any] = {
        "schema": RUNTIME_SAMPLES_SCHEMA,
        "scheduler": scheduler,
        "tools": tools,
        "trace": trace,
    }
    samples_path = benchmark_root / "samples.json"
    _freeze_json(samples_path, samples, label="runtime samples")

    inventory = _inventory_payload(benchmark_root, (samples_path, *trace_paths))
    _freeze_json(benchmark_root / "inventory.json", inventory, label="runtime inventory")
    summary = _summarize_samples(samples)
    report = _report_payload(manifest, inventory, summary)
    _freeze_json(benchmark_root / "report.json", report, label="runtime report")
    return RuntimeBenchmarkReport(
        root=benchmark_root,
        manifest_digest=str(manifest["manifest_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(report["report_digest"]),
        summary=summary,
    )


def rebuild_runtime_benchmark(root: str | Path) -> RuntimeBenchmarkReport:
    """Verify receipts and rebuild the runtime summary from frozen raw samples."""
    benchmark_root = Path(root).resolve()
    manifest = _read_object(benchmark_root / "manifest.json")
    _verify_embedded_digest(manifest, "manifest_digest", RUNTIME_MANIFEST_SCHEMA)
    samples = _read_object(benchmark_root / "samples.json")
    if samples.get("schema") != RUNTIME_SAMPLES_SCHEMA:
        raise ValueError(f"unsupported runtime samples: {samples.get('schema')!r}")
    inventory = _read_object(benchmark_root / "inventory.json")
    _verify_embedded_digest(inventory, "inventory_digest", RUNTIME_INVENTORY_SCHEMA)
    _verify_inventory(benchmark_root, inventory)

    summary = _summarize_samples(samples)
    expected = _report_payload(manifest, inventory, summary)
    stored = _read_object(benchmark_root / "report.json")
    _verify_embedded_digest(stored, "report_digest", RUNTIME_REPORT_SCHEMA)
    if _canonical_digest(stored) != _canonical_digest(expected):
        raise ValueError("runtime report does not match the frozen samples")
    return RuntimeBenchmarkReport(
        root=benchmark_root,
        manifest_digest=str(manifest["manifest_digest"]),
        inventory_digest=str(inventory["inventory_digest"]),
        report_digest=str(stored["report_digest"]),
        summary=summary,
    )


async def _scheduler_samples(config: RuntimeBenchmarkConfig) -> dict[str, Any]:
    runner = _SchedulerRunner()
    scheduler = TurnScheduler(
        runner,
        foreground_limit=config.foreground_limit,
        background_limit=config.background_limit,
        max_queued=config.scheduler_requests,
    )
    requests = [
        TurnRequest(
            id=f"turn-{index:05d}",
            session_id=f"session-{index % config.scheduler_sessions:03d}",
            content=str(index // config.scheduler_sessions),
            lane="background" if index % 5 == 0 else "foreground",
            metadata={"sequence": index // config.scheduler_sessions},
        )
        for index in range(config.scheduler_requests)
    ]
    started = perf_counter()
    handles = [scheduler.submit(request) for request in requests]
    results = await asyncio.gather(*(handle.result() for handle in handles))
    wall_ms = (perf_counter() - started) * 1000
    await asyncio.sleep(0)
    accepted = scheduler.accepted_count
    completed = scheduler.completed_count
    await scheduler.shutdown()
    request_by_id = {request.id: request for request in requests}
    rows = [
        {
            "request_id": result.request_id,
            "session_id": result.session_id,
            "sequence": request_by_id[result.request_id].metadata["sequence"],
            "status": result.status,
            "queue_latency_ms": result.queue_latency_ms,
            "end_to_end_latency_ms": max(
                0.0,
                result.finished_at - request_by_id[result.request_id].submitted_at,
            )
            * 1000,
        }
        for result in results
    ]
    return {
        "requests": rows,
        "started_by_session": dict(sorted(runner.started_by_session.items())),
        "accepted_count": accepted,
        "completed_count": completed,
        "max_active": runner.max_active,
        "wall_ms": wall_ms,
    }


async def _tool_samples(config: RuntimeBenchmarkConfig) -> dict[str, Any]:
    sequential: list[dict[str, Any]] = []
    parallel: list[dict[str, Any]] = []
    await _measure_tool_batch(config.tool_calls, config.tool_delay_ms, "sequential")
    await _measure_tool_batch(config.tool_calls, config.tool_delay_ms, "parallel")
    for _ in range(config.tool_repeats):
        sequential.append(
            await _measure_tool_batch(config.tool_calls, config.tool_delay_ms, "sequential")
        )
        parallel.append(
            await _measure_tool_batch(config.tool_calls, config.tool_delay_ms, "parallel")
        )
    return {
        "kind": "synthetic_async_read",
        "delay_ms_per_call": config.tool_delay_ms,
        "calls_per_batch": config.tool_calls,
        "sequential": sequential,
        "parallel": parallel,
    }


async def _trace_samples(
    root: Path,
    config: RuntimeBenchmarkConfig,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    trace_dir = root / "traces"
    baselines: list[float] = []
    traced: list[float] = []
    paths: list[Path] = []
    for index in range(config.trace_repeats):
        baseline = await _measure_tool_batch(config.tool_calls, 0.0, "parallel")
        path = trace_dir / f"request-{index:03d}.jsonl"
        recorder = TraceRecorder(path, session_id=f"benchmark-{index}")
        measured = await _measure_tool_batch(
            config.tool_calls,
            0.0,
            "parallel",
            listener=recorder,
        )
        baselines.append(float(baseline["duration_ms"]))
        traced.append(float(measured["duration_ms"]))
        paths.append(path)
    return (
        {
            "fsync": True,
            "baseline_duration_ms": baselines,
            "traced_duration_ms": traced,
            "overhead_ms": [
                max(0.0, traced_value - baseline)
                for baseline, traced_value in zip(baselines, traced, strict=True)
            ],
            "file_bytes": [path.stat().st_size for path in paths],
            "span_counts": [len(TraceRecorder(path, fsync=False).read_all()) for path in paths],
        },
        tuple(paths),
    )


async def _measure_tool_batch(
    call_count: int,
    delay_ms: float,
    mode: str,
    *,
    listener: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    active = 0
    max_active = 0

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: object = None,
        on_update: object = None,
    ) -> AgentToolResult:
        nonlocal active, max_active
        del arguments, signal, on_update
        active += 1
        max_active = max(max_active, active)
        try:
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)
            else:
                await asyncio.sleep(0)
            return AgentToolResult(content=[TextContent(text=tool_call_id)])
        finally:
            active -= 1

    calls = [ToolCall(id=f"read-{index}", name="read", arguments={}) for index in range(call_count)]
    tool_message = AssistantMessage(
        content=list[AssistantContent](calls),
        model="benchmark",
        stop_reason="toolUse",
    )
    final_message = AssistantMessage(
        content=[TextContent(text="done")],
        model="benchmark",
        stop_reason="stop",
    )
    provider = FakeProvider(
        [
            [
                AssistantStartEvent(partial=AssistantMessage(model="benchmark")),
                *(
                    ToolCallEndEvent(content_index=index, tool_call=call, partial=tool_message)
                    for index, call in enumerate(calls)
                ),
                AssistantDoneEvent(reason="toolUse", message=tool_message),
            ],
            [
                AssistantStartEvent(partial=AssistantMessage(model="benchmark")),
                AssistantDoneEvent(reason="stop", message=final_message),
            ],
        ]
    )
    result_order: list[str] = []
    started = perf_counter()
    async for event in run_agent_loop(
        provider=provider,
        model="benchmark",
        system="runtime benchmark",
        messages=[UserMessage(content="read inputs")],
        tools=[
            AgentTool(
                name="read",
                label="Read",
                description="Synthetic async read benchmark.",
                parameters={"type": "object"},
                execute_fn=execute,
            )
        ],
        tool_execution="sequential" if mode == "sequential" else "parallel",
        max_parallel_tools=call_count,
    ):
        if isinstance(event, MessageEndEvent) and isinstance(event.message, ToolResultMessage):
            result_order.append(event.message.tool_call_id)
        if listener is not None:
            await listener(event)
    return {
        "duration_ms": (perf_counter() - started) * 1000,
        "result_order": result_order,
        "max_active": max_active,
    }


def _summarize_samples(samples: Mapping[str, Any]) -> dict[str, Any]:
    scheduler = _mapping(samples, "scheduler")
    rows = _sequence_of_mappings(scheduler, "requests")
    ids = [str(row["request_id"]) for row in rows]
    expected_ids = {f"turn-{index:05d}" for index in range(len(rows))}
    observed_ids = set(ids)
    missing = expected_ids - observed_ids
    duplicates = len(ids) - len(observed_ids)
    failures = sum(row.get("status") != "succeeded" for row in rows)
    queue_latencies = [float(row.get("queue_latency_ms") or 0.0) for row in rows]
    end_to_end = [float(row.get("end_to_end_latency_ms") or 0.0) for row in rows]
    started_by_session = _mapping(scheduler, "started_by_session")
    order_violations = sum(
        list(values) != sorted(values)
        for values in started_by_session.values()
        if isinstance(values, list)
    )
    request_count = len(rows)
    wall_ms = float(scheduler.get("wall_ms", 0.0))

    tools = _mapping(samples, "tools")
    sequential_rows = _sequence_of_mappings(tools, "sequential")
    parallel_rows = _sequence_of_mappings(tools, "parallel")
    sequential_ms = [float(row["duration_ms"]) for row in sequential_rows]
    parallel_ms = [float(row["duration_ms"]) for row in parallel_rows]
    expected_order = [f"read-{index}" for index in range(int(tools["calls_per_batch"]))]
    tool_order_violations = sum(
        list(row.get("result_order", [])) != expected_order
        for row in (*sequential_rows, *parallel_rows)
    )
    sequential_p50 = percentile(sequential_ms, 0.50)
    parallel_p50 = percentile(parallel_ms, 0.50)

    trace = _mapping(samples, "trace")
    overhead = [float(value) for value in _sequence(trace, "overhead_ms")]
    file_bytes = [int(value) for value in _sequence(trace, "file_bytes")]
    span_counts = [int(value) for value in _sequence(trace, "span_counts")]
    return {
        "scheduler": {
            "requests": request_count,
            "accepted": int(scheduler.get("accepted_count", 0)),
            "completed": int(scheduler.get("completed_count", 0)),
            "missing": len(missing),
            "duplicates": duplicates,
            "failed": failures,
            "session_order_violations": order_violations,
            "acceptance_rate": (
                (request_count - failures - len(missing) - duplicates) / request_count
                if request_count
                else 0.0
            ),
            "max_active": int(scheduler.get("max_active", 0)),
            "wall_ms": wall_ms,
            "throughput_requests_per_second": request_count / (wall_ms / 1000) if wall_ms else 0.0,
            "queue_p50_ms": percentile(queue_latencies, 0.50),
            "queue_p95_ms": percentile(queue_latencies, 0.95),
            "end_to_end_p50_ms": percentile(end_to_end, 0.50),
            "end_to_end_p95_ms": percentile(end_to_end, 0.95),
        },
        "tools": {
            "kind": str(tools.get("kind", "unknown")),
            "calls_per_batch": int(tools.get("calls_per_batch", 0)),
            "repeats": len(sequential_ms),
            "sequential_p50_ms": sequential_p50,
            "sequential_p95_ms": percentile(sequential_ms, 0.95),
            "parallel_p50_ms": parallel_p50,
            "parallel_p95_ms": percentile(parallel_ms, 0.95),
            "median_latency_reduction": (
                1 - parallel_p50 / sequential_p50 if sequential_p50 else 0.0
            ),
            "order_violations": tool_order_violations,
            "sequential_max_active": max(
                (int(row.get("max_active", 0)) for row in sequential_rows),
                default=0,
            ),
            "parallel_max_active": max(
                (int(row.get("max_active", 0)) for row in parallel_rows),
                default=0,
            ),
        },
        "trace": {
            "requests": len(overhead),
            "fsync": bool(trace.get("fsync", False)),
            "overhead_p50_ms": percentile(overhead, 0.50),
            "overhead_p95_ms": percentile(overhead, 0.95),
            "mean_bytes_per_request": sum(file_bytes) / len(file_bytes) if file_bytes else 0.0,
            "mean_spans_per_request": sum(span_counts) / len(span_counts) if span_counts else 0.0,
        },
    }


def _manifest_payload(config: RuntimeBenchmarkConfig) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "repository": repository_evidence(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "config": asdict(config),
        "methodology": {
            "scheduler": "production TurnScheduler with an asyncio-yielding deterministic runner",
            "tools": "production agent loop with a synthetic fixed-delay async read tool",
            "trace": "production TraceRecorder with fsync enabled, paired against an untraced loop",
        },
    }
    payload["manifest_digest"] = _canonical_digest(payload)
    return payload


def _inventory_payload(root: Path, paths: Sequence[Path]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": RUNTIME_INVENTORY_SCHEMA,
        "files": {
            path.relative_to(root).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path.read_bytes()),
            }
            for path in sorted(paths, key=str)
        },
    }
    payload["inventory_digest"] = _canonical_digest(payload)
    return payload


def _report_payload(
    manifest: Mapping[str, Any],
    inventory: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": RUNTIME_REPORT_SCHEMA,
        "manifest_digest": manifest["manifest_digest"],
        "inventory_digest": inventory["inventory_digest"],
        "summary": summary,
    }
    payload["report_digest"] = _canonical_digest(payload)
    return payload


def _verify_inventory(root: Path, inventory: Mapping[str, Any]) -> None:
    files = _mapping(inventory, "files")
    for relative, receipt_value in files.items():
        if not isinstance(receipt_value, Mapping):
            raise ValueError(f"invalid inventory receipt for {relative}")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"runtime evidence file is missing or outside its root: {relative}")
        if path.stat().st_size != int(receipt_value.get("bytes", -1)):
            raise ValueError(f"runtime evidence size mismatch: {relative}")
        if _sha256(path.read_bytes()) != receipt_value.get("sha256"):
            raise ValueError(f"runtime evidence digest mismatch: {relative}")


def _verify_embedded_digest(
    payload: Mapping[str, Any],
    digest_key: str,
    schema: str,
) -> None:
    if payload.get("schema") != schema:
        raise ValueError(f"unsupported runtime evidence schema: {payload.get('schema')!r}")
    if payload.get(digest_key) != _digest_without(payload, digest_key):
        raise ValueError(f"runtime evidence {digest_key} does not match its content")


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"runtime evidence field {key!r} must be an object")
    return value


def _sequence(payload: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"runtime evidence field {key!r} must be an array")
    return value


def _sequence_of_mappings(
    payload: Mapping[str, Any],
    key: str,
) -> list[Mapping[str, Any]]:
    values = _sequence(payload, key)
    if any(not isinstance(value, Mapping) for value in values):
        raise ValueError(f"runtime evidence field {key!r} must contain objects")
    return [value for value in values if isinstance(value, Mapping)]


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
    "RUNTIME_INVENTORY_SCHEMA",
    "RUNTIME_MANIFEST_SCHEMA",
    "RUNTIME_REPORT_SCHEMA",
    "RUNTIME_SAMPLES_SCHEMA",
    "RuntimeBenchmarkConfig",
    "RuntimeBenchmarkReport",
    "rebuild_runtime_benchmark",
    "run_runtime_benchmarks",
]
