"""Evidence-backed evaluation campaign CLI for Run Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from run_agent_coding.thinking import normalize_thinking_level
from run_agent_evals.campaign import CampaignConfig, EvaluationCampaign, rebuild_campaign
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import load_tasks
from run_agent_evals.runtime_bench import (
    RuntimeBenchmarkConfig,
    rebuild_runtime_benchmark,
    run_runtime_benchmarks,
)


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(Path.cwd() / ".env", override=False)
    tasks = load_tasks(args.tasks)
    requested_model = args.model or os.environ.get("MODEL")
    requested_thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    root = args.output_root or Path(".run") / "evals" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    executor = CodingTaskExecutor(
        root / "runtime",
        provider_name=args.provider,
        model=requested_model,
        thinking_level_override=(
            normalize_thinking_level(requested_thinking) if requested_thinking else None
        ),
        extension_paths=tuple(path.resolve() for path in args.extension),
        project_extensions_enabled=args.project_extensions,
        trust_default="always" if args.trust_project else "never",
    )
    report = await EvaluationCampaign(
        root,
        CampaignConfig(
            candidate_id=args.candidate_id,
            seeds=tuple(args.seed or (0,)),
            concurrency=args.concurrency,
            keep_workspaces=args.keep_workspaces,
            metadata={
                "provider": args.provider or "default",
                "model": requested_model or "default",
                "thinking": requested_thinking or "default",
            },
        ),
    ).run(tasks, executor)
    print(json.dumps(asdict(report.summary), ensure_ascii=False, indent=2))
    print(f"Evidence: {report.root}")
    return 0 if report.summary.errored == 0 else 2


def _rebuild(args: argparse.Namespace) -> int:
    report = rebuild_campaign(args.output_root)
    print(json.dumps(asdict(report.summary), ensure_ascii=False, indent=2))
    print(f"Evidence verified: {report.root}")
    return 0


async def _runtime(args: argparse.Namespace) -> int:
    root = args.output_root or Path(".run") / "benchmarks" / "runtime" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    report = await run_runtime_benchmarks(
        root,
        RuntimeBenchmarkConfig(
            scheduler_requests=args.scheduler_requests,
            scheduler_sessions=args.scheduler_sessions,
            foreground_limit=args.foreground_limit,
            background_limit=args.background_limit,
            tool_calls=args.tool_calls,
            tool_repeats=args.tool_repeats,
            tool_delay_ms=args.tool_delay_ms,
            trace_repeats=args.trace_repeats,
        ),
    )
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    print(f"Evidence: {report.root}")
    return 0


def _runtime_rebuild(args: argparse.Namespace) -> int:
    report = rebuild_runtime_benchmark(args.output_root)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    print(f"Evidence verified: {report.root}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-agent-bench",
        description="Run or rebuild evidence-backed Run Agent evaluation campaigns.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Execute a frozen task campaign.")
    run.add_argument("tasks", type=Path)
    run.add_argument("--output-root", type=Path)
    run.add_argument("--candidate-id", default="baseline")
    run.add_argument("--seed", type=int, action="append")
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument("--provider")
    run.add_argument("--model")
    run.add_argument("--thinking")
    run.add_argument("--extension", type=Path, action="append", default=[])
    run.add_argument("--keep-workspaces", action="store_true")
    run.add_argument("--project-extensions", action="store_true")
    run.add_argument("--trust-project", action="store_true")
    rebuild = commands.add_parser("rebuild", help="Verify and reduce existing artifacts.")
    rebuild.add_argument("output_root", type=Path)
    runtime = commands.add_parser(
        "runtime",
        help="Benchmark scheduler, parallel tools, and tracing with frozen evidence.",
    )
    runtime.add_argument("--output-root", type=Path)
    runtime.add_argument("--scheduler-requests", type=int, default=10_000)
    runtime.add_argument("--scheduler-sessions", type=int, default=100)
    runtime.add_argument("--foreground-limit", type=int, default=32)
    runtime.add_argument("--background-limit", type=int, default=8)
    runtime.add_argument("--tool-calls", type=int, default=8)
    runtime.add_argument("--tool-repeats", type=int, default=9)
    runtime.add_argument("--tool-delay-ms", type=float, default=20.0)
    runtime.add_argument("--trace-repeats", type=int, default=9)
    runtime_rebuild = commands.add_parser(
        "runtime-rebuild",
        help="Verify a frozen runtime benchmark and rebuild its summary.",
    )
    runtime_rebuild.add_argument("output_root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "rebuild":
            return _rebuild(args)
        if args.command == "runtime":
            return asyncio.run(_runtime(args))
        return _runtime_rebuild(args)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Evaluation failed: {exc}") from exc


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
