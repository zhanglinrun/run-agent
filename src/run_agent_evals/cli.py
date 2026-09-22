"""Evidence-backed evaluation campaign CLI for Run Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from run_agent_coding.context_view import ContextStrategy
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.thinking import normalize_thinking_level
from run_agent_evals.campaign import CampaignConfig, EvaluationCampaign, rebuild_campaign
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.context_bench import (
    CompactionStrategyExecutor,
    rebuild_context_benchmark,
    rebuild_task_context_benchmark,
    run_context_benchmark,
    run_task_context_benchmark,
)
from run_agent_evals.evolution import (
    EVOLUTION_ABLATIONS,
    EVOLUTION_ARMS,
    AblationName,
    CodingExecutor,
    EvolutionArm,
    EvolutionArmEvaluationService,
    EvolutionArmRefused,
    EvolutionArmReport,
    EvolutionArmRequest,
    EvolutionEvaluationService,
    rebuild_evolution_comparison,
    rebuild_evolution_report,
    write_evolution_comparison,
)
from run_agent_evals.runner import TaskExecutor
from run_agent_evals.runtime_bench import (
    RuntimeBenchmarkConfig,
    rebuild_runtime_benchmark,
    run_runtime_benchmarks,
)
from run_agent_evals.suite import report_for_directory
from run_agent_evals.task_loading import load_tasks
from run_agent_extensions import resolve_extension_path
from run_agent_extensions.experience.candidates import CandidateError, ProjectProbe, SkillCandidate
from run_agent_extensions.experience.config import load_experience_config
from run_agent_extensions.experience.evolution import EvolutionPolicy, SkillEvolution
from run_agent_extensions.experience.memory import MemoryScope
from run_agent_extensions.experience.stores import ExperienceStores


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
        extension_paths=tuple(resolve_extension_path(path).resolve() for path in args.extension),
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


def _context(args: argparse.Namespace) -> int:
    if args.output_root is not None and args.output_root_flag is not None:
        raise ValueError("context output root may be supplied only once")
    root = args.output_root_flag or args.output_root
    if root is None:
        root = (
            Path(".run") / "benchmarks" / "context" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )
    report = run_context_benchmark(root)
    print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    if args.tasks is not None:
        task_report = asyncio.run(
            run_task_context_benchmark(
                root,
                load_tasks(args.tasks),
                executor_factory=_context_task_executors(args, root),
            )
        )
        print(json.dumps(task_report.summary, ensure_ascii=False, indent=2))
    print(f"Evidence: {report.root}")
    return 0


def _context_task_executors(
    args: argparse.Namespace, root: Path
) -> Callable[[ContextStrategy], TaskExecutor]:
    """Build one real coding executor per compaction strategy for the task benchmark."""
    state_root = args.state_root or root / "runtime"
    thinking = normalize_thinking_level(args.thinking) if args.thinking else None

    def factory(strategy: ContextStrategy) -> TaskExecutor:
        executor = CodingTaskExecutor(
            state_root / strategy,
            provider_name=args.provider,
            model=args.model,
            thinking_level_override=thinking,
        )
        return CompactionStrategyExecutor(executor, strategy)

    return factory


def _context_rebuild(args: argparse.Namespace) -> int:
    root = Path(args.output_root).resolve()
    reports = []
    if (root / "evidence.json").is_file():
        reports.append(rebuild_context_benchmark(root))
    if (root / "tasks-evidence.json").is_file():
        reports.append(rebuild_task_context_benchmark(root))
    if not reports:
        raise ValueError(f"No context benchmark evidence under {root}")
    for report in reports:
        print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    print(f"Evidence verified: {root}")
    return 0


def _candidate_for(
    stores: ExperienceStores,
    *,
    scope: MemoryScope,
    name: str,
    candidate_id: str | None,
) -> SkillCandidate:
    if candidate_id is not None:
        candidate = stores.candidates.require(candidate_id)
        if candidate.scope != scope or candidate.name != name:
            raise CandidateError(
                f"candidate {candidate_id} belongs to {candidate.scope}/{candidate.name}"
            )
        return candidate
    pending = next(
        (
            item
            for item in stores.candidates.list()
            if item.scope == scope and item.name == name and item.status in {"cold", "verified"}
        ),
        None,
    )
    if pending is None:
        raise CandidateError(f"no pending candidate found for {scope}/{name}")
    return pending


def _experience_stores(args: argparse.Namespace) -> ExperienceStores:
    home = args.state_root.resolve() if args.state_root else RunAgentPaths().home
    paths = RunAgentPaths(
        home=home,
        agents_home=(home / ".agents" if args.state_root else RunAgentPaths().agents_home),
    )
    return ExperienceStores.resolve(
        paths,
        Path.cwd(),
        config=load_experience_config(os.environ),
        project_enabled=args.trust_project,
    )


async def _evolve(args: argparse.Namespace) -> int:
    load_dotenv(Path.cwd() / ".env", override=False)
    if args.ablate and not args.arm:
        raise ValueError("--ablate needs --arm gated-evolution in the same campaign")
    stores = _experience_stores(args)
    if args.arm:
        return await _evolve_arms(args, stores)
    config = stores.config
    scope = cast(MemoryScope, args.scope)
    candidate = _candidate_for(
        stores,
        scope=scope,
        name=args.skill,
        candidate_id=args.candidate_id,
    )
    requested_thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    executor = CodingExecutor(
        provider_name=args.provider,
        model=args.model or os.environ.get("MODEL"),
        thinking_level_override=(
            normalize_thinking_level(requested_thinking) if requested_thinking else None
        ),
    )
    service = EvolutionEvaluationService(
        suite=args.suite,
        output_root=args.output_root,
        candidates=stores.candidates,
        skills=stores.skills,
        executor=executor,
    )
    policy = EvolutionPolicy(
        suite=service.suite.family,
        suite_version=service.suite.version,
        budget_seconds=args.budget_seconds or config.evolution_budget_seconds,
    )
    evolution = SkillEvolution(
        candidates=stores.candidates,
        skills=stores.skills,
        probe=ProjectProbe(Path.cwd(), trusted=args.trust_project),
        evaluation=service,
        project_enabled=args.trust_project,
        policy=policy,
        config=config,
    )
    evaluated = await evolution.evaluate(candidate.candidate_id)
    if evaluated.report_id is None:
        raise RuntimeError("evolution evaluation produced no report")
    report = await service.report(evaluated.report_id)
    publication = None
    if report.passed:
        publication = (await evolution.publish(candidate.candidate_id)).message
    root = service.output_root / report.report_id
    print(
        json.dumps(
            {
                "candidate_id": candidate.candidate_id,
                "status": stores.candidates.require(candidate.candidate_id).status,
                "passed": report.passed,
                "summary": report.summary,
                "publication": publication,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Evidence: {root}")
    return 0 if report.passed else 2


def _evolution_arm_requests(
    args: argparse.Namespace, stores: ExperienceStores
) -> list[EvolutionArmRequest]:
    arms = list(dict.fromkeys(cast(list[str], args.arm)))
    ablations = list(dict.fromkeys(cast(list[str], args.ablate)))
    if ablations and "gated-evolution" not in arms:
        raise ValueError("--ablate needs --arm gated-evolution in the same campaign")
    candidate_arms = [arm for arm in arms if arm in {"gated-evolution", "ungated-revision"}]
    candidate_id: str | None = args.candidate_id
    if candidate_arms and candidate_id is None:
        raise ValueError("gated-evolution and ungated-revision need an explicit --candidate-id")
    if not candidate_arms and candidate_id is not None:
        raise ValueError("--candidate-id applies only to gated-evolution and ungated-revision")
    scope = cast(MemoryScope, args.scope)
    if candidate_arms:
        candidate_id = _candidate_for(
            stores, scope=scope, name=args.skill, candidate_id=candidate_id
        ).candidate_id
    budget = args.budget_seconds or stores.config.evolution_budget_seconds
    requests: list[EvolutionArmRequest] = []
    for arm in arms:
        is_candidate_arm = arm in {"gated-evolution", "ungated-revision"}
        requests.append(
            EvolutionArmRequest(
                arm=cast(EvolutionArm, arm),
                skill=args.skill,
                scope=scope,
                candidate_id=candidate_id if is_candidate_arm else None,
                budget_seconds=budget,
            )
        )
        if arm == "gated-evolution":
            requests.extend(
                EvolutionArmRequest(
                    arm="gated-evolution",
                    skill=args.skill,
                    scope=scope,
                    candidate_id=candidate_id,
                    ablation=cast(AblationName, name),
                    budget_seconds=budget,
                )
                for name in ablations
            )
    return requests


async def _evolve_arms(args: argparse.Namespace, stores: ExperienceStores) -> int:
    """Measure frozen arms and ablations; never publish and never advance a candidate."""
    requests = _evolution_arm_requests(args, stores)
    requested_thinking = args.thinking or os.environ.get("REASONING_EFFORT")
    executor = CodingExecutor(
        provider_name=args.provider,
        model=args.model or os.environ.get("MODEL"),
        thinking_level_override=(
            normalize_thinking_level(requested_thinking) if requested_thinking else None
        ),
    )
    service = EvolutionArmEvaluationService(
        suite=args.suite,
        output_root=args.output_root,
        candidates=stores.candidates,
        skills=stores.skills,
        executor=executor,
        concurrency=args.concurrency,
        probe=ProjectProbe(Path.cwd(), trusted=True) if args.trust_project else None,
        report_roots=tuple(cast(list[Path], args.report_root)),
    )
    reports: list[EvolutionArmReport] = []
    refusals: list[dict[str, str]] = []
    for request in requests:
        try:
            reports.append(await service.evaluate(request))
        except EvolutionArmRefused as exc:
            refusals.append(
                {
                    "arm": request.arm,
                    "ablation": request.ablation or "",
                    "reason": str(exc),
                }
            )
    if not reports:
        raise RuntimeError(f"no arm produced evidence: {refusals[0]['reason']}")
    comparison = write_evolution_comparison(service.output_root, reports)
    if refusals:
        (service.output_root / "refusals.json").write_text(
            json.dumps(
                {"schema": "run-agent.evolution-refusals.v1", "refused": refusals},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "arms": [
                    {
                        key: arm[key]
                        for key in (
                            "label",
                            "arm",
                            "ablation",
                            "report",
                            "report_id",
                            "passed",
                            "non_product",
                            "totals",
                        )
                    }
                    for arm in cast(list[dict[str, object]], comparison["arms"])
                ],
                "refused": refusals,
                "comparison": str(service.output_root / "comparison.json"),
                "report": str(service.output_root / "REPORT.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Evidence: {service.output_root}")
    return 2 if refusals else 0


def _evolve_rebuild(args: argparse.Namespace) -> int:
    root = args.output_root.resolve()
    if (root / "comparison.json").is_file():
        comparison = rebuild_evolution_comparison(root)
        print(
            json.dumps(
                {
                    "arms": [
                        {
                            "label": arm["label"],
                            "report_id": arm["report_id"],
                            "passed": arm["passed"],
                            "totals": arm["totals"],
                        }
                        for arm in cast(list[dict[str, object]], comparison["arms"])
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"Evidence verified: {root}")
        return 0
    if not (root / "report.json").is_file():
        candidates = [path.parent for path in root.glob("*/report.json")]
        if len(candidates) != 1:
            raise ValueError("evolve-rebuild needs a report directory or a unique report child")
        root = candidates[0]
    report = rebuild_evolution_report(root)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Evidence verified: {root}")
    return 0


async def _runtime(args: argparse.Namespace) -> int:
    root = args.output_root or Path(".run") / "benchmarks" / "runtime" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    report = await run_runtime_benchmarks(
        root,
        RuntimeBenchmarkConfig(
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


def _suite(args: argparse.Namespace) -> int:
    """Grade every ready task in a directory through the dual propositions."""
    report = report_for_directory(args.tasks, use_reference=not args.candidate_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["rate"]["successes"] == report["rate"]["trials"] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run bench",
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
    suite = commands.add_parser(
        "suite",
        help="Grade every ready task in a task directory through the dual propositions.",
    )
    suite.add_argument("tasks", type=Path, help="directory holding tasks.json and task folders")
    suite.add_argument(
        "--candidate-root",
        type=Path,
        help="reserved for agent-produced workspaces; absent means the reference solution",
    )
    runtime = commands.add_parser(
        "runtime",
        help="Microbenchmark parallel tools and tracing with frozen evidence.",
    )
    runtime.add_argument("--output-root", type=Path)
    runtime.add_argument("--tool-calls", type=int, default=8)
    runtime.add_argument("--tool-repeats", type=int, default=9)
    runtime.add_argument("--tool-delay-ms", type=float, default=20.0)
    runtime.add_argument("--trace-repeats", type=int, default=9)
    runtime_rebuild = commands.add_parser(
        "runtime-rebuild",
        help="Verify a frozen runtime benchmark and rebuild its summary.",
    )
    runtime_rebuild.add_argument("output_root", type=Path)
    context = commands.add_parser(
        "context",
        help="Compare summary-only and cheap-first context preparation offline.",
    )
    context.add_argument("output_root", nargs="?", type=Path)
    context.add_argument("--output-root", dest="output_root_flag", type=Path)
    context.add_argument(
        "--tasks",
        type=Path,
        help="JSONL task manifest; also run the cheap-first/summary-only coding comparison.",
    )
    context.add_argument(
        "--state-root",
        type=Path,
        help="Session/telemetry root for task trials (default: <output-root>/runtime).",
    )
    context.add_argument("--provider")
    context.add_argument("--model")
    context.add_argument("--thinking")
    context_rebuild = commands.add_parser(
        "context-rebuild",
        help="Verify context benchmark evidence and rebuild its report offline.",
    )
    context_rebuild.add_argument("output_root", type=Path)
    evolve = commands.add_parser(
        "evolve",
        help="Evaluate and publish one pending Skill candidate through paired hidden graders.",
    )
    evolve.add_argument("suite", type=Path)
    evolve.add_argument("--skill", required=True)
    evolve.add_argument("--scope", choices=("user", "project"), default="user")
    evolve.add_argument("--candidate-id", "--candidate", dest="candidate_id")
    evolve.add_argument("--state-root", type=Path)
    evolve.add_argument("--output-root", type=Path, required=True)
    evolve.add_argument("--provider")
    evolve.add_argument("--model")
    evolve.add_argument("--thinking")
    evolve.add_argument("--budget-seconds", type=float)
    evolve.add_argument("--trust-project", action="store_true")
    evolve.add_argument(
        "--arm",
        action="append",
        choices=EVOLUTION_ARMS,
        default=[],
        help=(
            "Measure one frozen arm (repeatable): no-skill, static-skill, "
            "ungated-revision or gated-evolution. Arm evidence never publishes."
        ),
    )
    evolve.add_argument(
        "--ablate",
        action="append",
        choices=EVOLUTION_ABLATIONS,
        default=[],
        help=(
            "Remove one gate from the gated-evolution arm (repeatable): "
            "project-probe refuses candidates that cite project facts; "
            "behavior-gate keeps structure and fact checks but skips the paired gate."
        ),
    )
    evolve.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Parallel arm/ablation trials with one isolated state root each; arms only.",
    )
    evolve.add_argument(
        "--report-root",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Extra root searched for the passing paired report the gated-evolution arm "
            "needs; --output-root is always searched too."
        ),
    )
    evolve_rebuild = commands.add_parser(
        "evolve-rebuild",
        help="Verify frozen Skill-evolution evidence and rebuild its gate offline.",
    )
    evolve_rebuild.add_argument("output_root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "rebuild":
            return _rebuild(args)
        if args.command == "suite":
            return _suite(args)
        if args.command == "runtime":
            return asyncio.run(_runtime(args))
        if args.command == "runtime-rebuild":
            return _runtime_rebuild(args)
        if args.command == "context":
            return _context(args)
        if args.command == "context-rebuild":
            return _context_rebuild(args)
        if args.command == "evolve":
            return asyncio.run(_evolve(args))
        return _evolve_rebuild(args)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Evaluation failed: {exc}") from exc


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
