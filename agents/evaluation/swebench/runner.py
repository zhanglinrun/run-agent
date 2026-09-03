"""SWE-bench Verified prediction campaign and artifact writer."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from ...providers.config import resolve_api_config
from ...providers.probe import probe_model
from ...harness import (
    AgentHarness,
    ExecutionSettings,
    ExtensionSettings,
    PermissionSettings,
    ProviderSettings,
    RuntimeConfig,
    SessionSettings,
    TaskResult,
    TaskSpec,
    TaskStatus,
    VerificationSettings,
)
from ...runtime.contracts import utc_now
from ...runtime.tracing import load_trace, trace_digest
from ...verification.discovery import VerificationCommand
from ...execution import (
    DockerSandboxBackend,
    SandboxError,
    SandboxSpec,
    prepare_workspace_for_container,
)
from .adapter import (
    DATASET_ID,
    DATASET_SPLIT,
    DEFAULT_DATASET_PATH,
    EXPECTED_SHA256,
    checkout_instance,
    download_swebench_verified,
    export_host_patch,
    load_swebench_verified,
    select_instances,
    sha256_file,
)
from .evaluator import official_case_status, run_official_grader
from .manifest import git_head, image_digest, policy_hash, tool_schema_hash
from ..campaign import campaign_budget, disabled_harness_extensions
from ..model_config import resolve_campaign_model
from .models import SWEBenchInstance
from ..compare import compare_predictions
from ..modes import HARNESS_MODES


PI_REWRITE_INSTANCES = (
    "django__django-14672",
    "sphinx-doc__sphinx-10449",
    "django__django-11299",
    "django__django-14493",
    "django__django-11551",
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _task_image(instance: SWEBenchInstance, override: str | None) -> tuple[str, str]:
    if override:
        return override, "/workspace"
    if instance.image:
        return instance.image, "/testbed"
    return "run-agent-python-sandbox:latest", "/workspace"


def _focused_verification_commands(instance: SWEBenchInstance) -> tuple[VerificationCommand, ...] | None:
    """Extract only the repository test command from an official eval script.

    The script also contains environment setup, test-patch application, and
    package installation commands.  Those are deliberately excluded: runtime
    verification must not install packages or apply hidden benchmark tests.
    """
    for raw_line in instance.eval_script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            continue
        if not tokens or Path(tokens[0]).name != "runtests.py":
            continue
        return (
            VerificationCommand(
                "benchmark-focused-tests",
                (sys.executable, "tests/runtests.py", *tokens[1:]),
                240.0,
                focused=True,
            ),
        )
    return None


def _cost_from_tokens(tokens: dict[str, Any], args: argparse.Namespace) -> float | None:
    input_rate = getattr(args, "input_cost_per_million", None)
    output_rate = getattr(args, "output_cost_per_million", None)
    if input_rate is None and output_rate is None:
        return None
    if input_rate is None or output_rate is None:
        raise ValueError("set both --input-cost-per-million and --output-cost-per-million")
    return round(
        (float(tokens.get("input", 0) or 0) * input_rate
         + float(tokens.get("output", 0) or 0) * output_rate) / 1_000_000,
        8,
    )


def _rewrite_scored_results(
    results_path: Path,
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    updated: list[dict[str, Any]] = []
    temporary = results_path.with_suffix(results_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        for row in rows:
            status = official_case_status(report, str(row["instance_id"]))
            row["official_status"] = status["status"]
            if status["status"] in {"resolved", "unresolved", "error", "empty_patch"}:
                row["correct"] = status["status"] == "resolved"
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            updated.append(row)
    temporary.replace(results_path)
    return updated


def _write_mechanism_report(run_dir: Path, results_path: Path) -> Path:
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if results_path.exists() else []
    lines = [
        "# Harness Mechanism Report",
        "",
        "This five-case report is mechanism evidence, not a statistically significant benchmark claim.",
        "",
        "| Instance | First Patch | Verification | Fingerprint | Correction | Final Patch | Official | Failure |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        candidates = row.get("patch_candidates") if isinstance(row.get("patch_candidates"), list) else []
        corrections = row.get("correction_attempts") if isinstance(row.get("correction_attempts"), list) else []
        if candidates:
            first_patch = str(candidates[0].get("patch_sha256") or "-")[:12]
        elif corrections:
            # Preserve the pre-repair candidate even when a custom runner did
            # not persist PatchCandidate metadata.
            first_patch = str(corrections[0].get("before_patch_sha256") or "-")[:12]
        else:
            # Baseline intentionally skips runtime verification, so it has no
            # PatchCandidate list. Its final non-empty patch is still the
            # first (and only) candidate and belongs in the report.
            final_sha = str(row.get("final_patch_sha256") or "")
            first_patch = final_sha[:12] if final_sha else "-"
        history = row.get("verification_history") if isinstance(row.get("verification_history"), list) else []
        first_verification = history[0] if history else {}
        verification = str(first_verification.get("outcome") or row.get("verification_outcome") or "not_run")
        fingerprint = str(first_verification.get("fingerprint") or row.get("verification_fingerprint") or "-")[:16]
        correction = ", ".join(str(item.get("action") or "attempt") for item in corrections) or "not_triggered"
        final_patch = str(row.get("final_patch_sha256") or "-")[:12]
        official = str(row.get("official_status") or "pending")
        failure = row.get("failure") if isinstance(row.get("failure"), dict) else {}
        failure_kind = str(failure.get("kind") or ("infrastructure_failure" if row.get("infrastructure_failure") else "-"))
        lines.append(
            f"| {row.get('instance_id')} | `{first_patch}` | {verification} | `{fingerprint}` | "
            f"{correction} | `{final_patch}` | {official} | {failure_kind} |"
        )
        for item in corrections:
            lines.extend([
                "",
                f"- `{row.get('instance_id')}` repair {item.get('attempt')}: "
                f"`{str(item.get('before_patch_sha256') or '-')[:12]}` → "
                f"`{str(item.get('after_patch_sha256') or '-')[:12]}`; "
                f"action=`{item.get('action')}`.",
            ])
    report = run_dir / "mechanism_report.md"
    report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report


async def run_swebench_campaign(args: argparse.Namespace) -> Path:
    adapter_only = bool(getattr(args, "adapter_only", False))
    if adapter_only and getattr(args, "grade", False):
        raise ValueError("--adapter-only cannot be combined with --grade")

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        dataset_path = download_swebench_verified(dataset_path)
    dataset_sha256 = sha256_file(dataset_path)
    if dataset_sha256 != EXPECTED_SHA256:
        raise RuntimeError(
            f"SWE-bench Verified dataset hash mismatch: expected {EXPECTED_SHA256}, got {dataset_sha256}"
        )
    instances = select_instances(
        load_swebench_verified(dataset_path),
        limit=args.limit,
        seed=args.seed,
        instance_ids=args.instance_id,
    )
    if not instances:
        raise ValueError("no SWE-bench instances selected")

    base_url = api_key = None
    use_openai = False
    if not adapter_only:
        base_url, api_key, use_openai = resolve_api_config(cli_api_base=args.api_base)
        if not api_key:
            raise RuntimeError("API key is required for a live SWE-bench campaign")
        if getattr(args, "max_cost", None) is None:
            raise ValueError("live SWE-bench campaign requires an explicit --max-cost limit")
    model = resolve_campaign_model(args.model, adapter_only=adapter_only)
    probe = (
        {"skipped": True, "mode": "adapter-only"}
        if adapter_only
        else await probe_model(model=model, api_key=api_key, base_url=base_url, use_openai=use_openai)
    )
    _cost_from_tokens({}, args)

    sandbox_mode = getattr(args, "sandbox", "docker")
    if sandbox_mode not in {"local", "docker"}:
        raise ValueError("sandbox must be local or docker")
    harness_mode = getattr(args, "harness_mode", "full")
    if harness_mode not in HARNESS_MODES:
        raise ValueError(f"unsupported harness mode: {harness_mode}")
    harness_flags = HARNESS_MODES[harness_mode]
    sandbox_image_override = getattr(args, "sandbox_image", None)

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = Path(args.output).resolve() / f"swebench-verified-{stamp}-{model.replace('/', '_')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    predictions_path = run_dir / "predictions.jsonl"
    results_path = run_dir / "results.jsonl"
    for directory in ("patches", "traces", "logs", "tasks"):
        (run_dir / directory).mkdir(parents=True, exist_ok=True)

    configured_images = sorted({_task_image(item, sandbox_image_override)[0] for item in instances}) \
        if sandbox_mode == "docker" else []
    image_digests = {image: image_digest(image) for image in configured_images}
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "benchmark": DATASET_ID,
        "split": DATASET_SPLIT,
        "dataset": str(dataset_path.resolve()),
        "dataset_sha256": dataset_sha256,
        "model": model,
        "protocol": "adapter-only" if adapter_only else ("openai" if use_openai else "anthropic"),
        "provider_base_url_host": (
            "adapter-only" if adapter_only else (urlparse(base_url).netloc if base_url else "provider-default")
        ),
        "model_probe": probe,
        "seed": args.seed,
        "selected_count": len(instances),
        "selected_instance_ids": [item.instance_id for item in instances],
        "run_config": {
            "permission_mode": args.permission_mode,
            "max_turns": args.max_turns,
            "max_cost_usd_per_task": args.max_cost,
            "pricing_usd_per_million": {
                "input": args.input_cost_per_million,
                "output": args.output_cost_per_million,
            },
            "temperature": args.temperature,
            "thinking": args.thinking,
            "memory_enabled": False,
            "skills_enabled": False,
            "context_compaction_enabled": True,
            "harness_mode": harness_mode,
            "runtime_verification_enabled": harness_flags["verification"],
            "correction_enabled": harness_flags["correction"],
            "max_repair_attempts": args.max_repair_attempts,
            "execution_backend": sandbox_mode,
            "adapter_only": adapter_only,
            "sandbox_image_override": sandbox_image_override,
            "network": args.network,
            "memory_mb": args.memory_mb,
            "cpus": args.cpus,
            "pids_limit": args.pids_limit,
            "patch_timeout_seconds": args.patch_timeout,
        },
        "git_commit": git_head(Path.cwd()),
        "tool_schema_sha256": tool_schema_hash(),
        "policy_sha256": policy_hash(Path.cwd()),
        "sandbox_images": image_digests,
        "sandbox_image_digests": sorted({digest for digest in image_digests.values() if digest}),
        "verification_config": {
            "official_harness": True,
            "runtime_verification": harness_flags["verification"],
            "correction": harness_flags["correction"],
        },
        "started_at": utc_now(),
        "status": "running",
    }
    _write_json(run_dir / "manifest.json", manifest)

    rows: list[dict[str, Any]] = []
    campaign_status = "completed"
    campaign_error: str | None = None
    try:
        for index, instance in enumerate(instances, start=1):
            started = time.perf_counter()
            task_dir = run_dir / "tasks" / instance.instance_id
            trace_dir = task_dir / "traces"
            task_dir.mkdir(parents=True, exist_ok=True)
            _write_json(task_dir / "prompt.json", {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "problem_statement": instance.problem_statement,
                "hints_text": instance.hints_text,
            })

            error: str | None = None
            patch = ""
            infrastructure_failure = False
            result = TaskResult(instance.instance_id, TaskStatus.FAILED)
            session = None
            sandbox_log: dict[str, Any] = {
                "backend": sandbox_mode,
                "requested": sandbox_mode == "docker",
                "started": False,
                "closed": sandbox_mode == "local",
                "timed_out": False,
            }

            with tempfile.TemporaryDirectory(prefix=f"run-agent-swe-{instance.instance_id}-") as temporary:
                workspace = Path(temporary) / "workspace"
                try:
                    checkout_instance(instance, workspace)
                    if sandbox_mode == "docker":
                        prepare_workspace_for_container(workspace)

                    spec = None
                    if sandbox_mode == "docker":
                        image, container_workspace = _task_image(instance, sandbox_image_override)
                        spec = SandboxSpec(
                            workspace=workspace,
                            image=image,
                            network=args.network,
                            memory_mb=args.memory_mb,
                            cpus=args.cpus,
                            pids_limit=args.pids_limit,
                            timeout_seconds=args.eval_timeout,
                            patch_timeout_seconds=args.patch_timeout,
                            container_workspace=container_workspace,
                            python_executable=(
                                "/opt/miniconda3/envs/testbed/bin/python"
                                if container_workspace == "/testbed" else "python"
                            ),
                            verification_profile="python-swebench",
                            run_id=run_dir.name,
                            case_id=instance.instance_id,
                        )
                        sandbox_log.update({
                            "image": image,
                            "container_workspace": container_workspace,
                        })

                    if adapter_only:
                        if spec is not None:
                            session = await DockerSandboxBackend().start(spec)
                            snapshot = getattr(session, "lifecycle_snapshot", None)
                            if callable(snapshot):
                                sandbox_log.update(snapshot())
                            else:
                                sandbox_log.update({
                                    "started": True,
                                    "container_id": getattr(session, "container_id", None),
                                })
                        patch = export_host_patch(workspace)
                        result = TaskResult(instance.instance_id, TaskStatus.COMPLETED, patch=patch)
                    else:
                        result = await AgentHarness().run(TaskSpec(
                            task_id=instance.instance_id,
                            prompt=instance.prompt(),
                            workspace=workspace,
                            mode="swebench",
                            verification_profile="python-swebench",
                            budget=campaign_budget(args),
                            runtime=RuntimeConfig(
                                provider=ProviderSettings(
                                    model=model,
                                    api_key=api_key,
                                    api_base=base_url,
                                    use_openai=use_openai,
                                    temperature=args.temperature,
                                    thinking=args.thinking,
                                ),
                                permissions=PermissionSettings(mode=args.permission_mode),
                                execution=ExecutionSettings(
                                    backend=sandbox_mode,
                                    sandbox_spec=spec,
                                    allow_host_shell=bool(args.allow_host_shell),
                                ),
                                session=SessionSettings(
                                    artifact_dir=task_dir / "harness",
                                    trace_root=trace_dir,
                                ),
                                verification=VerificationSettings(
                                    commands=_focused_verification_commands(instance),
                                    disposable_workspace=True,
                                ),
                                extensions=ExtensionSettings(
                                    disabled=disabled_harness_extensions(
                                        harness_flags,
                                        include_plan=True,
                                    ),
                                    load_user=False,
                                ),
                            ),
                            metadata={"benchmark": DATASET_ID},
                        ))
                        patch = result.patch
                        harness_sandbox = result.metadata.get("sandbox")
                        if isinstance(harness_sandbox, dict):
                            sandbox_log.update(harness_sandbox)
                        infrastructure_failure = result.status == TaskStatus.INFRASTRUCTURE_FAILURE
                        if result.failure is not None:
                            error = f"{result.failure.kind.value}: {result.failure.message}"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    infrastructure_failure = isinstance(exc, SandboxError)
                finally:
                    try:
                        if session is not None:
                            await session.close()
                            snapshot = getattr(session, "lifecycle_snapshot", None)
                            if callable(snapshot):
                                sandbox_log.update(snapshot())
                    except Exception as exc:
                        infrastructure_failure = True
                        error = f"{type(exc).__name__}: {exc}"

            _write_json(task_dir / "sandbox.log", sandbox_log)
            _write_json(task_dir / "verification.json", {
                "status": result.verification.outcome if result.verification is not None else "not_run",
                "runtime_verification_enabled": harness_flags["verification"],
                "correction_enabled": harness_flags["correction"],
                "report": result.verification.to_dict() if result.verification is not None else None,
                "official_grading": "pending" if args.grade else "not_run",
            })
            patch_path = task_dir / "patch.diff"
            patch_path.write_text(patch, encoding="utf-8")
            shutil.copyfile(patch_path, run_dir / "patches" / f"{instance.instance_id}.diff")
            if result.trace_path and result.trace_path.exists():
                shutil.copyfile(result.trace_path, run_dir / "traces" / f"{instance.instance_id}.jsonl")
            shutil.copyfile(task_dir / "sandbox.log", run_dir / "logs" / f"{instance.instance_id}.sandbox.log")
            _write_json(task_dir / "official_result.json", {
                "status": "pending" if args.grade else "not_run",
                "reason": "official SWE-bench harness owns final grading",
            })
            if error:
                _write_json(task_dir / "error.json", {
                    "error": error,
                    "infrastructure_failure": infrastructure_failure,
                })

            _append_jsonl(predictions_path, instance.prediction(patch, model_name=model))
            trace_files = sorted(trace_dir.glob("*.jsonl"))
            adapter_ok = bool(adapter_only and not infrastructure_failure and not error)
            row = {
                "index": index,
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "error": error,
                "infrastructure_failure": infrastructure_failure,
                "adapter_ok": adapter_ok,
                "patch": str(patch_path),
                "patch_bytes": len(patch.encode("utf-8")),
                "final_patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch else None,
                "task_status": result.status.value,
                "tokens": result.usage.to_dict(),
                "cost_usd": _cost_from_tokens(result.usage.to_dict(), args),
                "repair_attempts": len(result.correction_attempts),
                "runtime_verification_failed": bool(result.verification and result.verification.outcome != "PASS"),
                "sandbox_timed_out": bool(sandbox_log.get("timed_out")),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "traces": [
                    {"path": str(path), "sha256": trace_digest(load_trace(path))}
                    for path in trace_files
                ],
                "verification": str(task_dir / "verification.json"),
                "sandbox_log": str(task_dir / "sandbox.log"),
                "session_id": result.session_id or None,
                "session_db": result.metadata.get("session_db"),
                "session_db_sha256": result.metadata.get("session_db_sha256"),
                "failure": result.failure.to_dict() if result.failure else None,
                "correction_attempts": [item.__dict__ for item in result.correction_attempts],
                "patch_candidates": result.metadata.get("candidates", []),
                "verification_history": result.metadata.get("verification_history", []),
                "verification_outcome": result.verification.outcome if result.verification else None,
                "verification_fingerprint": result.verification.fingerprint if result.verification else None,
                "correction_activated": bool(result.correction_attempts),
            }
            rows.append(row)
            _append_jsonl(results_path, row)
            status = "ADAPTER_OK" if adapter_ok else ("PATCH" if patch else "EMPTY")
            print(f"[{index}/{len(instances)}] {instance.instance_id} {status}")
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        campaign_status = "interrupted"
        campaign_error = type(exc).__name__
        raise
    except BaseException as exc:
        campaign_status = "failed"
        campaign_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        manifest.update({
            "completed_at": utc_now(),
            "status": campaign_status,
            "error": campaign_error,
            "completed_cases": len(rows),
            "predictions": str(predictions_path),
            "results": str(results_path),
            "nonempty_patches": sum(bool(item["patch_bytes"]) for item in rows),
            "infrastructure_failures": sum(bool(item["infrastructure_failure"]) for item in rows),
            "adapter_ok_cases": sum(bool(item.get("adapter_ok")) for item in rows) if adapter_only else None,
            "sandbox_images": {
                image: image_digest(image) or image_digests.get(image)
                for image in configured_images
            },
        })
        manifest["sandbox_image_digests"] = sorted({
            digest for digest in manifest["sandbox_images"].values() if digest
        })
        _write_json(run_dir / "manifest.json", manifest)
        _write_mechanism_report(run_dir, results_path)

    if args.grade:
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            report_path = run_official_grader(predictions_path, instances, run_dir, args)
        except Exception as exc:
            manifest["status"] = "grading_failed"
            manifest["official_grader"] = {"report": None, "error": f"{type(exc).__name__}: {exc}"}
            _write_json(manifest_path, manifest)
            for instance in instances:
                _write_json(run_dir / "tasks" / instance.instance_id / "official_result.json", {
                    "status": "grading_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            raise
        manifest["official_grader"] = {"report": str(report_path) if report_path else None}
        if not report_path or not report_path.exists():
            error = "official SWE-bench grader completed without producing a report"
            manifest["status"] = "grading_failed"
            manifest["official_grader"]["error"] = error
            _write_json(manifest_path, manifest)
            for instance in instances:
                _write_json(run_dir / "tasks" / instance.instance_id / "official_result.json", {
                    "status": "grading_failed",
                    "error": error,
                })
            raise RuntimeError(error)
        if report_path and report_path.exists():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                scored_rows = _rewrite_scored_results(results_path, report)
                manifest["official_grader"].update({
                    "total_instances": report.get("total_instances"),
                    "resolved_instances": report.get("resolved_instances"),
                    "resolved_rate": round(
                        report["resolved_instances"] / report["total_instances"], 6
                    ) if report.get("total_instances") else None,
                })
                resolved_rows = [row for row in scored_rows if row.get("correct") is True]
                repair_successes = [row for row in resolved_rows if int(row.get("repair_attempts", 0) or 0) > 0]
                first_passes = [row for row in resolved_rows if int(row.get("repair_attempts", 0) or 0) == 0]
                costs = [float(row["cost_usd"]) for row in scored_rows if row.get("cost_usd") is not None]
                manifest["metrics"] = {
                    "resolved_rate": len(resolved_rows) / len(scored_rows) if scored_rows else 0.0,
                    "first_pass_rate": len(first_passes) / len(scored_rows) if scored_rows else 0.0,
                    "repair_success_rate": len(repair_successes) / len(scored_rows) if scored_rows else 0.0,
                    "mean_repair_attempts": (
                        sum(int(row.get("repair_attempts", 0) or 0) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                    "mean_input_tokens": (
                        sum(int((row.get("tokens") or {}).get("input", 0) or 0) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                    "mean_output_tokens": (
                        sum(int((row.get("tokens") or {}).get("output", 0) or 0) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                    "mean_cost_usd": sum(costs) / len(costs) if costs else None,
                    "mean_duration_ms": (
                        sum(float(row.get("duration_ms", 0) or 0) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                    "infrastructure_failure_rate": (
                        sum(bool(row.get("infrastructure_failure")) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                    "timeout_rate": (
                        sum(bool(row.get("sandbox_timed_out")) for row in scored_rows) / len(scored_rows)
                        if scored_rows else 0.0
                    ),
                }
                for instance in instances:
                    task_result = official_case_status(report, instance.instance_id)
                    task_result["official_report"] = str(report_path)
                    _write_json(run_dir / "tasks" / instance.instance_id / "official_result.json", task_result)
                _write_mechanism_report(run_dir, results_path)
            except (OSError, json.JSONDecodeError, TypeError, ZeroDivisionError):
                manifest["official_grader"]["report_parse_error"] = True
        _write_json(manifest_path, manifest)
    return run_dir


async def run_pi_rewrite_ablation(args: argparse.Namespace) -> Path:
    """Run the fixed five-case, three-arm mechanism experiment sequentially."""

    if not getattr(args, "grade", False):
        raise ValueError("the fixed pi-rewrite ablation requires --grade so paired results use official scores")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dirs: dict[str, Path] = {}
    for mode in ("baseline", "verifier", "full"):
        values = vars(args).copy()
        values.update({
            "command": "campaign",
            "output": str(root / mode),
            "model": args.model or "gpt-5.6-luna",
            "limit": None,
            "seed": 42,
            "instance_id": list(PI_REWRITE_INSTANCES),
            "temperature": 0.0,
            "max_turns": 18,
            "max_repair_attempts": 2,
            "max_workers": 1,
            "harness_mode": mode,
            "adapter_only": False,
            "network": "none",
        })
        run_dirs[mode] = await run_swebench_campaign(argparse.Namespace(**values))

    allowed = {"harness_mode", "runtime_verification_enabled", "correction_enabled"}
    comparisons = {
        "baseline_vs_verifier": compare_predictions(
            run_dirs["baseline"] / "results.jsonl",
            run_dirs["verifier"] / "results.jsonl",
            seed=42,
            allowed_manifest_differences=allowed,
        ),
        "baseline_vs_full": compare_predictions(
            run_dirs["baseline"] / "results.jsonl",
            run_dirs["full"] / "results.jsonl",
            seed=42,
            allowed_manifest_differences=allowed,
        ),
        "verifier_vs_full": compare_predictions(
            run_dirs["verifier"] / "results.jsonl",
            run_dirs["full"] / "results.jsonl",
            seed=42,
            allowed_manifest_differences=allowed,
        ),
    }
    _write_json(root / "paired_comparison.json", {
        "schema_version": 1,
        "created_at": utc_now(),
        "purpose": "five-case mechanism validation; not a statistical benchmark claim",
        "instance_ids": list(PI_REWRITE_INSTANCES),
        "arms": {name: str(path) for name, path in run_dirs.items()},
        "comparisons": comparisons,
    })
    return root


def _add_campaign_arguments(parser: argparse.ArgumentParser, *, default_output: str) -> None:
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--output", default=default_output)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--instance-id", action="append")
    parser.add_argument(
        "--permission-mode",
        default="acceptEdits",
        choices=["default", "acceptEdits", "dontAsk", "bypassPermissions"],
    )
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--input-cost-per-million", type=float, default=3.0)
    parser.add_argument("--output-cost-per-million", type=float, default=15.0)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--grade", action="store_true")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--eval-timeout", type=int, default=1800)
    parser.add_argument("--harness-mode", choices=sorted(HARNESS_MODES), default="full")
    parser.add_argument("--max-repair-attempts", type=int, default=2)
    parser.add_argument("--sandbox", choices=["local", "docker"], default="docker")
    parser.add_argument(
        "--sandbox-image",
        default=None,
        help="Override the task image; formal runs default to each official SWE-bench image",
    )
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--allow-host-shell", action="store_true")
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--pids-limit", type=int, default=256)
    parser.add_argument("--patch-timeout", type=float, default=600.0)
    parser.add_argument("--network", default="none")
    parser.add_argument(
        "--adapter-only",
        action="store_true",
        help="Checkout/start sandbox and write artifacts without calling a model",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-agent-swebench", description="SWE-bench Verified adapter")
    subs = parser.add_subparsers(dest="command", required=True)

    download = subs.add_parser("download")
    download.add_argument("--output", default=str(DEFAULT_DATASET_PATH))
    download.add_argument("--overwrite", action="store_true")

    inspect = subs.add_parser("inspect")
    inspect.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH))
    inspect.add_argument("--limit", type=int, default=5)

    campaign = subs.add_parser("campaign")
    _add_campaign_arguments(campaign, default_output=".run/swebench-runs")

    ablation = subs.add_parser("pi-rewrite-ablation")
    _add_campaign_arguments(ablation, default_output=".run/swebench-pi-rewrite")
    return parser


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True) or None, override=False)
    args = build_parser().parse_args()
    if args.command == "download":
        path = download_swebench_verified(args.output, overwrite=args.overwrite)
        print(f"dataset={path}\nrows={len(load_swebench_verified(path))}\nsha256={sha256_file(path)}")
    elif args.command == "inspect":
        instances = load_swebench_verified(args.dataset)
        print(json.dumps({
            "dataset": args.dataset,
            "rows": len(instances),
            "instances": [
                {
                    "instance_id": item.instance_id,
                    "repo": item.repo,
                    "base_commit": item.base_commit,
                    "problem_statement_preview": " ".join(item.problem_statement.split())[:240],
                    "fail_to_pass_count": len(item.fail_to_pass),
                    "pass_to_pass_count": len(item.pass_to_pass),
                    "has_gold_patch": bool(item.gold_patch),
                    "gold_patch": "<redacted>",
                }
                for item in instances[: args.limit]
            ],
        }, ensure_ascii=False, indent=2))
    elif args.command == "campaign":
        print(f"run_dir={asyncio.run(run_swebench_campaign(args))}")
    else:
        print(f"run_dir={asyncio.run(run_pi_rewrite_ablation(args))}")


__all__ = [
    "HARNESS_MODES",
    "PI_REWRITE_INSTANCES",
    "build_parser",
    "main",
    "run_pi_rewrite_ablation",
    "run_swebench_campaign",
]
