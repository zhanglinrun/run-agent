"""Isolated coding-task benchmark runner.

GAIA/HLE measure broad agent ability.  This runner measures the actual product
claim: can the Harness modify a repository and satisfy environment tests?
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv

from ..harness import (
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
from ..providers.config import resolve_api_config
from ..runtime.contracts import utc_now
from ..providers.probe import probe_model
from .campaign import campaign_budget, disabled_harness_extensions
from .model_config import resolve_campaign_model
from ..verification import VerificationOrchestrator
from ..verification.discovery import VerificationCommand
from ..verification.models import VerificationReport
from ..execution import (
    DockerSandboxBackend,
    SandboxSpec,
    SandboxError,
    prepare_workspace_for_container,
    scrub_workspace_credentials,
)
from .modes import HARNESS_MODES


@dataclass(frozen=True)
class CodingTask:
    task_id: str
    prompt: str
    fixture: str
    verify: tuple[tuple[str, ...], ...]
    timeout_seconds: float = 180.0
    tags: tuple[str, ...] = ()


def load_coding_tasks(path: str | Path) -> list[CodingTask]:
    source = Path(path)
    tasks: list[CodingTask] = []
    for index, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        verify = value.get("verify") or []
        commands: list[tuple[str, ...]] = []
        for item in verify:
            if not isinstance(item, list) or not item:
                raise ValueError(f"task line {index} verify commands must be non-empty argv arrays")
            commands.append(tuple(str(arg) for arg in item))
        task_id = str(value.get("id") or value.get("task_id") or f"task-{index}")
        fixture = Path(str(value.get("fixture") or ""))
        if not fixture.is_absolute():
            fixture = (source.parent / fixture).resolve()
        tasks.append(
            CodingTask(
                task_id=task_id,
                prompt=str(value.get("prompt") or ""),
                fixture=str(fixture),
                verify=tuple(commands),
                timeout_seconds=float(value.get("timeout_seconds") or 180.0),
                tags=tuple(str(tag) for tag in value.get("tags") or []),
            )
        )
    return tasks


def _git_patch(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _initialize_fixture_repository(root: Path) -> None:
    commands = (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        (
            "git",
            "-c",
            "user.name=Run Agent Eval",
            "-c",
            "user.email=eval@run-agent.local",
            "commit",
            "-q",
            "-m",
            "fixture baseline",
        ),
    )
    for argv in commands:
        result = subprocess.run(
            list(argv), cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to initialize fixture repository: {' '.join(argv)}\n{result.stderr}")


async def _verify(root: Path, task: CodingTask, *, sandbox=None):
    commands = [
        VerificationCommand(f"acceptance-{index + 1}", argv, task.timeout_seconds, focused=True)
        for index, argv in enumerate(task.verify)
    ]
    return await VerificationOrchestrator(workspace_root=root, commands=commands, sandbox=sandbox).verify([])


async def run_coding_campaign(args: argparse.Namespace) -> Path:
    tasks = load_coding_tasks(args.tasks)
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        raise ValueError("no coding tasks selected")
    adapter_only = bool(getattr(args, "adapter_only", False))
    base_url = api_key = None
    use_openai = False
    if not adapter_only:
        base_url, api_key, use_openai = resolve_api_config(cli_api_base=args.api_base)
        if not api_key:
            raise RuntimeError("API key is required for a live coding benchmark")
        if getattr(args, "max_cost", None) is None:
            raise ValueError("live coding campaign requires an explicit --max-cost limit")
    model = resolve_campaign_model(args.model, adapter_only=adapter_only)
    probe = (
        {"ok": False, "mode": "adapter-only"}
        if adapter_only
        else await probe_model(model=model, api_key=api_key, base_url=base_url, use_openai=use_openai)
    )
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = Path(args.output).resolve() / f"coding-{stamp}-{model.replace('/', '_')}"
    run_dir.mkdir(parents=True, exist_ok=False)
    results_path = run_dir / "results.jsonl"
    passed = 0
    adapter_ok_cases = 0
    sandbox_mode = getattr(args, "sandbox", "docker")
    if sandbox_mode not in {"local", "docker"}:
        raise ValueError("sandbox must be local or docker")
    harness_mode = getattr(args, "harness_mode", "full")
    harness_flags = HARNESS_MODES[harness_mode]
    try:
        for index, task in enumerate(tasks, start=1):
            fixture = Path(task.fixture)
            if not fixture.is_dir():
                raise FileNotFoundError(f"coding fixture not found: {fixture}")
            with tempfile.TemporaryDirectory(prefix=f"run-agent-{task.task_id}-") as temporary:
                workspace = Path(temporary) / "workspace"
                shutil.copytree(fixture, workspace)
                scrub_workspace_credentials(workspace)
                _initialize_fixture_repository(workspace)
                if getattr(args, "sandbox", "docker") == "docker":
                    # The Docker image runs as uid 1000.  Normalize only the
                    # disposable checkout; never change the user's fixture.
                    prepare_workspace_for_container(workspace)
                task_dir = run_dir / "tasks" / task.task_id
                trace_dir = task_dir / "traces"
                task_dir.mkdir(parents=True, exist_ok=True)
                patch = ""
                (task_dir / "prompt.json").write_text(
                    json.dumps({"task_id": task.task_id, "prompt": task.prompt}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                session = None
                baseline = None
                acceptance: VerificationReport | dict[str, Any] | None = None
                infrastructure_failure = False
                sandbox_spec = None
                sandbox_backend = None
                error = None
                sandbox_log: dict[str, Any] = {
                    "backend": sandbox_mode,
                    "requested": sandbox_mode == "docker",
                    "started": False,
                    "closed": sandbox_mode == "local",
                    "timed_out": False,
                }
                result = TaskResult(task.task_id, TaskStatus.FAILED)
                try:
                    if sandbox_mode == "docker":
                        sandbox_spec = SandboxSpec(
                            workspace=workspace,
                            image=getattr(args, "sandbox_image", "run-agent-python-sandbox:latest"),
                            network=getattr(args, "network", "none"),
                            memory_mb=getattr(args, "memory_mb", 2048),
                            cpus=getattr(args, "cpus", 2.0),
                            pids_limit=getattr(args, "pids_limit", 256),
                            timeout_seconds=task.timeout_seconds,
                            patch_timeout_seconds=getattr(args, "patch_timeout", 600.0),
                            run_id=run_dir.name,
                            case_id=task.task_id,
                        )
                        sandbox_backend = DockerSandboxBackend()
                        session = await sandbox_backend.start(sandbox_spec)
                        sandbox_log.update({
                            "started": True,
                            "container_id": getattr(session, "container_id", None),
                            "image": sandbox_spec.image,
                        })
                    baseline = await _verify(workspace, task, sandbox=session)
                    if adapter_only:
                        patch = await session.export_patch() if session is not None else _git_patch(workspace)
                        acceptance = await _verify(workspace, task, sandbox=session)
                        result = TaskResult(task.task_id, TaskStatus.COMPLETED, patch=patch)
                    else:
                        acceptance_commands = tuple(
                            VerificationCommand(
                                f"acceptance-{command_index + 1}",
                                argv,
                                task.timeout_seconds,
                                focused=True,
                            )
                            for command_index, argv in enumerate(task.verify)
                        )
                        result = await AgentHarness().run(TaskSpec(
                            task_id=task.task_id,
                            prompt=task.prompt,
                            workspace=workspace,
                            mode="coding",
                            verification_profile="python",
                            budget=campaign_budget(
                                args,
                                default_input_rate=3.0,
                                default_output_rate=15.0,
                            ),
                            runtime=RuntimeConfig(
                                provider=ProviderSettings(
                                    model=model,
                                    api_key=api_key,
                                    api_base=base_url,
                                    use_openai=use_openai,
                                    temperature=args.temperature,
                                ),
                                permissions=PermissionSettings(mode=args.permission_mode),
                                execution=ExecutionSettings(
                                    backend=sandbox_mode,
                                    sandbox_spec=sandbox_spec,
                                    sandbox_backend=sandbox_backend,
                                    sandbox_session=session,
                                    allow_host_shell=bool(args.allow_host_shell),
                                ),
                                session=SessionSettings(
                                    artifact_dir=task_dir / "harness",
                                    trace_root=trace_dir,
                                ),
                                verification=VerificationSettings(
                                    commands=acceptance_commands,
                                    acceptance_commands=acceptance_commands,
                                    disposable_workspace=True,
                                ),
                                extensions=ExtensionSettings(
                                    disabled=disabled_harness_extensions(
                                        harness_flags
                                    ),
                                    load_user=False,
                                ),
                            ),
                        ))
                        patch = result.patch
                        acceptance = result.metadata.get("acceptance")
                    infrastructure_failure = result.status == TaskStatus.INFRASTRUCTURE_FAILURE
                    if result.failure is not None:
                        error = f"{result.failure.kind.value}: {result.failure.message}"
                except Exception as exc:
                    infrastructure_failure = isinstance(exc, SandboxError)
                    error = f"{type(exc).__name__}: {exc}"
                finally:
                    if session is not None:
                        try:
                            if not bool(getattr(session, "closed", False)):
                                await session.close()
                        except Exception as exc:
                            infrastructure_failure = True
                            error = f"{type(exc).__name__}: {exc}"
                        snapshot = getattr(session, "lifecycle_snapshot", None)
                        if callable(snapshot):
                            sandbox_log.update(snapshot())
                        else:
                            sandbox_log.update({
                                "closed": bool(getattr(session, "closed", False)),
                                "timed_out": bool(getattr(session, "timed_out", False)),
                            })
                    harness_sandbox = result.metadata.get("sandbox")
                    if isinstance(harness_sandbox, dict):
                        sandbox_log.update(harness_sandbox)
                    (task_dir / "sandbox.log").write_text(
                        json.dumps(sandbox_log, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                if acceptance is None and not infrastructure_failure:
                    if sandbox_mode == "docker":
                        infrastructure_failure = True
                        error = error or "docker acceptance result missing from TaskResult"
                    else:
                        acceptance = await _verify(workspace, task)
                acceptance_dict = (
                    acceptance.to_dict() if isinstance(acceptance, VerificationReport)
                    else acceptance if isinstance(acceptance, dict)
                    else None
                )
                patch_path = task_dir / "patch.diff"
                patch_path.write_text(patch, encoding="utf-8")
                (task_dir / "verification.json").write_text(
                    json.dumps({
                        "baseline": baseline.to_dict() if baseline else None,
                        "acceptance": acceptance_dict,
                    }, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if error:
                    (task_dir / "error.json").write_text(
                        json.dumps({"error": error, "infrastructure_failure": infrastructure_failure},
                                   ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                acceptance_passed = bool(acceptance_dict and acceptance_dict.get("passed"))
                resolved = bool(not infrastructure_failure and baseline and not baseline.passed and acceptance_passed)
                adapter_ok = bool(adapter_only and not infrastructure_failure and baseline and acceptance and not error)
                passed += int(resolved)
                adapter_ok_cases += int(adapter_ok)
                row = {
                    "index": index,
                    "task": asdict(task),
                    "resolved": resolved,
                    "adapter_ok": adapter_ok,
                    "baseline": baseline.to_dict() if baseline else {"passed": False, "skipped_reason": "infrastructure_failure"},
                    "acceptance": acceptance_dict or {"passed": False, "skipped_reason": "infrastructure_failure"},
                    "error": error,
                    "infrastructure_failure": infrastructure_failure,
                    "task_status": result.status.value,
                    "tokens": result.usage.to_dict(),
                    "repair_attempts": len(result.correction_attempts),
                    "runtime_verification_failed": bool(result.verification and result.verification.outcome != "PASS"),
                    "sandbox_timed_out": bool(sandbox_log.get("timed_out")),
                    "trace": str(result.trace_path) if result.trace_path else None,
                    "session_id": result.session_id or None,
                    "session_db": result.metadata.get("session_db"),
                    "failure": result.failure.to_dict() if result.failure else None,
                    "patch": str(patch_path),
                }
                with results_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                status = "ADAPTER_OK" if adapter_ok else ("PASS" if resolved else "FAIL")
                print(f"[{index}/{len(tasks)}] {task.task_id} {status}")
    finally:
        manifest = {
            "schema_version": 1,
            "kind": "coding_task_campaign",
            "started_from": str(Path(args.tasks).resolve()),
            "completed_at": utc_now(),
            "model": model,
            "protocol": "adapter-only" if adapter_only else ("openai" if use_openai else "anthropic"),
            "provider_base_url_host": (
                "adapter-only" if adapter_only else (urlparse(base_url).netloc if base_url else "provider-default")
            ),
            "model_probe": probe,
            "permission_mode": args.permission_mode,
            "adapter_only": adapter_only,
            "harness_mode": harness_mode,
            "runtime_verification_enabled": harness_flags["verification"],
            "correction_enabled": harness_flags["correction"],
            "execution_backend": sandbox_mode,
            "sandbox": {
                "image": getattr(args, "sandbox_image", "run-agent-python-sandbox:latest"),
                "network": getattr(args, "network", "none"),
                "memory_mb": getattr(args, "memory_mb", 2048),
                "cpus": getattr(args, "cpus", 2.0),
                "pids_limit": getattr(args, "pids_limit", 256),
                "patch_timeout_seconds": getattr(args, "patch_timeout", 600.0),
            },
            "max_cost_usd": args.max_cost,
            "cases": len(tasks),
            "adapter_ok_cases": adapter_ok_cases if adapter_only else None,
            "resolved": passed,
            "resolved_rate": round(passed / len(tasks), 6),
            "results": str(results_path),
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-agent-coding-benchmark")
    parser.add_argument("tasks", help="JSONL coding-task manifest")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--output", default=".run/coding-benchmark-runs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--permission-mode", default="acceptEdits", choices=["default", "acceptEdits", "dontAsk", "bypassPermissions"])
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--input-cost-per-million", type=float, default=3.0)
    parser.add_argument("--output-cost-per-million", type=float, default=15.0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--sandbox", choices=["local", "docker"], default="docker")
    parser.add_argument("--sandbox-image", default="run-agent-python-sandbox:latest")
    parser.add_argument("--allow-host-shell", action="store_true")
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--cpus", type=float, default=2.0)
    parser.add_argument("--pids-limit", type=int, default=256)
    parser.add_argument("--patch-timeout", type=float, default=600.0)
    parser.add_argument("--network", default="none")
    parser.add_argument("--harness-mode", choices=["baseline", "verifier", "full"], default="full")
    parser.add_argument("--max-repair-attempts", type=int, default=2)
    parser.add_argument(
        "--adapter-only",
        action="store_true",
        help="Validate fixture, sandbox, verification, patch export and artifacts without calling a model",
    )
    return parser


def main() -> None:
    env_path = find_dotenv(usecwd=True)
    load_dotenv(env_path or None, override=False)
    run_dir = asyncio.run(run_coding_campaign(build_parser().parse_args()))
    print(f"run_dir={run_dir}")


if __name__ == "__main__":
    main()
