"""Isolated, checkpointed SWE-bench campaign; prepare never calls a paid model.

Use a NEW --campaign directory. Stages: prepare, preflight, solve, grade, status.
All solves import a hash-verified source snapshot, never the live source checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = ("experience", "mcp", "permission_policy", "plan_mode")
SAFE_FIELDS = ("instance_id", "repo", "base_commit", "problem_statement")
POLICY = {
    "RUN_AGENT_PERMISSION_MODE": "guarded",
    "RUN_AGENT_MCP_SERVERS": "{}",
    "EXPERIENCE_REVIEW_ENABLED": "true",
    "EXPERIENCE_MEMORY_ENABLED": "true",
    "EXPERIENCE_USER_PROFILE_ENABLED": "true",
    "EXPERIENCE_CURATOR_ENABLED": "true",
    "EXPERIENCE_SKILL_LEDGER": "true",
    "EXPERIENCE_MEMORY_WRITE_APPROVAL": "false",
    "EXPERIENCE_SKILLS_WRITE_APPROVAL": "false",
}
RULES = (
    "Fix the issue in this repository. Inspect the source, implement the fix and run relevant "
    "tests when possible. Work only in this checkout and the task-local experience stores. "
    "Do not access external benchmark answers, gold patches, grading data, other trials, "
    "or historical predictions. Do not modify .git or evaluation infrastructure. "
    "The evaluator will extract your source changes; finish with a concise summary.\n\n"
)
LOG_LOCK = threading.Lock()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, *, cwd=None, timeout=1800, env=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} exited {result.returncode}: {result.stderr[-2000:]}")
    return result.stdout


def sanitize_tasks(rows):
    tasks = [{key: row[key] for key in SAFE_FIELDS} for row in rows]
    ids = [row["instance_id"] for row in tasks]
    if len(tasks) != 50 or len(set(ids)) != 50:
        raise ValueError("Expected exactly 50 distinct benchmark tasks")
    for row in tasks:
        if row["repo"] not in {"django/django", "sphinx-doc/sphinx"}:
            raise ValueError("Unexpected repository")
        if not all(isinstance(value, str) and value for value in row.values()):
            raise ValueError("Invalid task field")
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in row["instance_id"]):
            raise ValueError("Unsafe instance ID")
        if len(row["base_commit"]) != 40 or any(
            c not in "0123456789abcdef" for c in row["base_commit"]
        ):
            raise ValueError("Expected full base commit hash")
    return tasks


def trials(tasks, samples):
    return [(sample, task) for sample in range(1, samples + 1) for task in tasks]


def inventory(root):
    return {
        path.relative_to(root).as_posix(): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def prepare(args):
    from dotenv import dotenv_values

    campaign = args.campaign
    if campaign.exists():
        raise FileExistsError("Prepare requires a fresh campaign directory; old artifacts retained")
    tasks = sanitize_tasks(read(args.dataset))
    campaign.mkdir(parents=True)
    frozen = campaign / "frozen"
    shutil.copytree(
        ROOT / "src", frozen / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copy2(__file__, frozen / "runner.py")
    shutil.copy2(ROOT / "pyproject.toml", frozen / "pyproject.toml")
    write(campaign / "tasks.json", tasks)
    files = inventory(frozen)
    write(campaign / "source-hashes.json", files)
    config = {
        "campaign_id": campaign.name,
        "created_at": time.time(),
        "model": args.model,
        "thinking": "max",
        "samples": args.samples,
        "concurrency": args.concurrency,
        "solve_timeout": args.solve_timeout,
        "review_timeout": args.review_timeout,
        "extensions": list(EXTENSIONS),
        "environment": dict(POLICY),
        "initial_experience": "empty and private per task/sample; no cross-trial learning",
        "tool_python_environment": "private per-trial venv; pip installs cannot alter the grader",
        "mcp_service_count": 0,
        "plan_initial_mode": "off",
        "review_cadence": "project defaults",
        "python": sys.executable,
        "grader_python": str(args.grader_python),
        "repo_root": str(ROOT),
        "repos": str(ROOT / ".run/swebench/repos"),
        "source_revision": run(["git", "rev-parse", "HEAD"], cwd=ROOT).strip(),
        "source_manifest_sha256": digest(campaign / "source-hashes.json"),
        "tasks_sha256": digest(campaign / "tasks.json"),
        "total_trials": len(trials(tasks, args.samples)),
        "dataset": "SWE-bench/SWE-bench_Verified",
        "subset": "HAL Verified Mini (local all50.json)",
        "cost_usd": None,
        "isolation": "independent shallow base-only Git repositories and private homes; "
        "no shared Git objects/refs; guarded policy is not an OS sandbox",
    }
    ambient = {**os.environ, **dotenv_values(ROOT / ".env")}
    safe_names = {
        "PROVIDER",
        "MODEL_CONTEXT_WINDOW",
        "MODEL_MAX_TOKENS",
        "MODEL_SUPPORTS_IMAGES",
        "OPENAI_API",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_RETRIES",
        "OPENAI_MAX_RETRY_DELAY_SECONDS",
        "ANTHROPIC_TIMEOUT_SECONDS",
        "ANTHROPIC_MAX_RETRIES",
        "ANTHROPIC_MAX_RETRY_DELAY_SECONDS",
    }
    config["environment"] = {
        key: value
        for key, value in ambient.items()
        if value is not None and (key in safe_names or key.startswith("EXPERIENCE_"))
    }
    config["environment"].update(POLICY)
    config["environment"].update(MODEL=args.model, REASONING_EFFORT="max")
    write(campaign / "config.json", config)
    (campaign / "dependencies.txt").write_text(
        run([sys.executable, "-m", "pip", "freeze"]), encoding="utf-8"
    )
    write(campaign / "freeze.json", {"config_sha256": digest(campaign / "config.json")})
    return config


def verify_freeze(campaign):
    config = read(campaign / "config.json")
    if digest(campaign / "config.json") != read(campaign / "freeze.json")["config_sha256"]:
        raise RuntimeError("Campaign configuration changed after freezing")
    if digest(campaign / "source-hashes.json") != config["source_manifest_sha256"]:
        raise RuntimeError("Source manifest changed")
    if inventory(campaign / "frozen") != read(campaign / "source-hashes.json"):
        raise RuntimeError("Frozen source changed; do not mix implementations in one campaign")
    if digest(campaign / "tasks.json") != config["tasks_sha256"]:
        raise RuntimeError("Sanitized task inputs changed")
    return config


def environment(config, trial=None):
    from dotenv import dotenv_values

    env = dict(os.environ)
    # Credentials are loaded in memory only; neither .env nor its contents enter trial files.
    values = dotenv_values(Path(config["repo_root"]) / ".env")
    prefixes = ("OPENAI_", "ANTHROPIC_", "MODEL", "PROVIDER", "REASONING_", "EXPERIENCE_")
    env.update(
        {
            key: value
            for key, value in values.items()
            if value is not None and key.startswith(prefixes)
        }
    )
    for key in list(env):
        if key.startswith(("EXPERIENCE_", "RUN_AGENT_MCP_", "RUN_AGENT_PERMISSION_")):
            env.pop(key)
    env.update(config["environment"])
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    if trial:
        env["RUN_AGENT_HOME"] = str(trial / "home")
        env["RUN_AGENT_AGENTS_HOME"] = str(trial / "agents")
        env["VIRTUAL_ENV"] = str(trial / "tool-env")
        env["PATH"] = str(trial / "tool-env" / "Scripts") + os.pathsep + env.get("PATH", "")
        env["PIP_REQUIRE_VIRTUALENV"] = "true"
        env.pop("PYTHONHOME", None)
    return env


def prepare_tool_environment(config, trial):
    """Keep solver dependency installs out of the harness and official-grader environments."""
    destination = trial / "tool-env"
    if destination.exists():
        raise FileExistsError(f"Tool environment already exists: {destination}")
    run([config["python"], "-m", "venv", "--system-site-packages", str(destination)])
    return destination / "Scripts/python.exe"


def redact(text, env):
    for key, value in env.items():
        if (
            value
            and len(value) >= 8
            and any(word in key.upper() for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ):
            text = text.replace(value, "[REDACTED]")
    return text


def event_record(event):
    """Retain stream deltas and complete final events without quadratic partial copies."""
    if event.type == "message_update":
        return {
            "type": event.type,
            "assistant_message_event": event.assistant_message_event.model_dump(
                mode="json", exclude={"partial"}
            ),
        }
    return event.model_dump(mode="json")


async def application_child(campaign, trial, *, probe=False):
    config = verify_freeze(campaign)
    sys.path.insert(0, str(campaign / "frozen/src"))
    from run_agent_coding.application import ApplicationOptions, CodingApplication
    from run_agent_coding.paths import RunAgentPaths
    from run_agent_core.messages import ToolCall
    from run_agent_extensions import BUILTIN_EXTENSIONS
    from run_agent_extensions.experience.config import load_experience_config

    if probe:
        from run_agent_core.messages import AssistantMessage, TextContent
        from run_agent_core.provider_events import AssistantDoneEvent

        class LocalProvider:
            async def stream_response(self, **kwargs):
                yield AssistantDoneEvent(
                    reason="stop",
                    message=AssistantMessage(
                        content=[TextContent(text="preflight")],
                        model="local",
                        provider="local",
                        stop_reason="stop",
                    ),
                )

        provider = LocalProvider()
    else:
        provider = None
    options = ApplicationOptions(
        cwd=trial / "work",
        paths=RunAgentPaths(home=trial / "home", agents_home=trial / "agents"),
        model=config["model"],
        thinking=config["thinking"],
        trust_default="always",
        extension_paths=tuple(BUILTIN_EXTENSIONS[name] for name in EXTENSIONS),
        trace_enabled=True,
    )
    async with await CodingApplication.open(options, provider=provider) as app:
        await app.start()
        runtime = app.session.extension_runtime
        sources = runtime.source_manifest()
        actual = {str(row["source_id"]).split("/")[-2] for row in sources}
        if actual != set(EXTENSIONS) or len(sources) != 4:
            raise RuntimeError(f"Extension activation mismatch: {actual}")
        tools = [tool.name for tool in app.session.tools]
        if not {"memory", "skill_manage"} <= set(tools):
            raise RuntimeError("Experience tools missing")
        plan = (await app.command("/plan status")).message
        if "off" not in plan:
            raise RuntimeError("Plan mode must start writable")
        blocked = await runtime.before_tool_call(
            ToolCall(
                id="probe",
                name="write",
                arguments={"path": str(trial / "outside.txt"), "content": "x"},
            )
        )
        if not blocked.block:
            raise RuntimeError("Guarded permission policy not active")
        manifest = {
            "sources": sources,
            "tools": tools,
            "plan": plan,
            "permission": "guarded",
            "outside_write_blocked": blocked.block,
            "mcp_service_count": 0,
            "experience_config": dataclasses.asdict(load_experience_config(runtime.environment)),
            "session_id": app.session.session_id,
            "private_home": str(trial / "home"),
        }
        write(trial / "activation.json", manifest)
        task = (
            {"problem_statement": "Reply with preflight."} if probe else read(trial / "input.json")
        )
        receipt = None

        async def consume():
            nonlocal receipt
            with (trial / "events.jsonl").open("a", encoding="utf-8") as output:
                async for event in app.prompt(RULES + task["problem_statement"]):
                    value = event_record(event)
                    output.write(redact(json.dumps(value, ensure_ascii=False), os.environ) + "\n")
                    output.flush()
                    if type(event).__name__ == "AgentSettledEvent":
                        receipt = value
                        write(trial / "session-receipt.json", receipt)

        try:
            await asyncio.wait_for(consume(), timeout=config["solve_timeout"])
        except TimeoutError:
            write(trial / "solve-timeout.json", {"timeout_seconds": config["solve_timeout"]})
            raise
        if receipt is None:
            raise RuntimeError("Solver exited without a durable completion receipt")
        review_source = next(
            row["source_id"] for row in sources if "/experience/" in str(row["source_id"])
        )
        services = runtime.host_services_for_source(review_source)
        started = time.monotonic()
        reviews = []
        while True:
            rows = await services.scope().state.list(prefix="review-task:")
            reviews = [await services.tasks.status(row.value["task_id"]) for row in rows]
            active = [
                row for row in reviews if row.status not in {"succeeded", "failed", "cancelled"}
            ]
            if not active:
                break
            if time.monotonic() - started >= config["review_timeout"]:
                reviews = [
                    await services.tasks.cancel(row.task_id) if row in active else row
                    for row in reviews
                ]
                break
            await asyncio.sleep(0.5)
        write(
            trial / "review.json",
            {
                "tasks": [dataclasses.asdict(row) for row in reviews],
                "wait_outcome": "no_trigger"
                if not reviews
                else ("timeout_cancel_requested" if active else "settled"),
                "wait_seconds": time.monotonic() - started,
                "status": (await app.command("/review status")).message,
            },
        )
        write(trial / "child-result.json", {"receipt": receipt, "cost_usd": None})


def preflight(campaign):
    config = verify_freeze(campaign)
    tasks = read(campaign / "tasks.json")
    root = campaign / "preflight" / str(time.time_ns())
    root.mkdir(parents=True)
    git_isolation = probe_git_isolation(root)
    tool_python = prepare_tool_environment(config, root)
    env = environment(config, root)
    tool_prefix = run([str(tool_python), "-c", "import sys; print(sys.prefix)"], env=env).strip()
    if Path(tool_prefix).resolve() != (root / "tool-env").resolve():
        raise RuntimeError("Solver tool Python is not isolated")
    result = subprocess.run(
        [
            config["python"],
            str(campaign / "frozen/runner.py"),
            "child",
            "--campaign",
            str(campaign),
            "--trial",
            str(root),
            "--probe",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    (root / "activation.log").write_text(
        redact(result.stdout + result.stderr, env), encoding="utf-8"
    )
    tags = [
        f"swebench/sweb.eval.x86_64.{task['instance_id'].replace('__', '_1776_')}:latest"
        for task in tasks
    ]
    images = {}
    for tag in tags:
        item = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            capture_output=True,
            text=True,
        )
        images[tag] = item.stdout.strip() if item.returncode == 0 else None
    grader = run(
        [
            config["grader_python"],
            "-c",
            "import importlib.metadata as m; import swebench.harness.run_evaluation; "
            "import datasets; print(m.version('swebench')); print(datasets.__version__)",
        ]
    )
    report = {
        "activation_exit_code": result.returncode,
        "images": images,
        "cached_images": sum(bool(value) for value in images.values()),
        "grader_versions": grader.strip(),
        "free_disk_bytes": shutil.disk_usage(campaign).free,
        "model_calls": 0,
        "tool_python_prefix": tool_prefix,
        "git_isolation": git_isolation,
        "ready": result.returncode == 0 and all(images.values()),
        "evidence": str(root),
    }
    write(root / "report.json", report)
    write(campaign / "preflight-latest.json", report)
    return report


def prepare_repository(source, work, base_commit):
    """Fetch only the base snapshot, never expose the cache's history or refs."""
    work.mkdir(parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    run(["git", "init", "--template="], cwd=work, env=env)
    run(["git", "config", "core.longpaths", "true"], cwd=work, env=env)
    run(
        ["git", "fetch", "--depth=1", "--no-tags", str(source.resolve()), base_commit],
        cwd=work,
        env=env,
    )
    run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=work, env=env)
    (work / ".git/FETCH_HEAD").unlink()
    if run(["git", "log", "--all", "--format=%H"], cwd=work, env=env).split() != [base_commit]:
        raise RuntimeError("Trial repository contains unexpected history")
    if run(["git", "status", "--porcelain"], cwd=work, env=env).strip():
        raise RuntimeError("New repository is not pristine")


def probe_git_isolation(root):
    """Exercise the real checkout path against a cache holding a future answer."""
    source = root / "synthetic-cache"
    source.mkdir()
    run(["git", "init", "--template="], cwd=source)
    commits = []
    for content in ("ancestor", "base", "future answer"):
        (source / "source.txt").write_text(content + "\n", encoding="utf-8")
        run(["git", "add", "source.txt"], cwd=source)
        run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                content,
            ],
            cwd=source,
        )
        commits.append(run(["git", "rev-parse", "HEAD"], cwd=source).strip())
    run(["git", "tag", "future-answer"], cwd=source)
    future_blob = run(["git", "rev-parse", "HEAD:source.txt"], cwd=source).strip()
    work = root / "work"
    prepare_repository(source, work, commits[1])
    objects = run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"], cwd=work
    ).split()
    if any(oid in objects for oid in (commits[0], commits[2], future_blob)):
        raise RuntimeError("Trial Git object database exposes history outside the base snapshot")
    if (
        run(["git", "remote", "-v"], cwd=work).strip()
        or run(["git", "for-each-ref", "--format=%(refname)"], cwd=work).strip()
    ):
        raise RuntimeError("Trial repository exposes source refs or remotes")
    if any(
        (work / ".git" / name).exists()
        for name in ("objects/info/alternates", "objects/info/http-alternates", "FETCH_HEAD")
    ):
        raise RuntimeError("Trial repository retains source breadcrumbs")
    return {
        "base_commit": commits[1],
        "future_commit_inaccessible": True,
        "future_blob_inaccessible": True,
        "ancestor_inaccessible": True,
        "refs_and_remotes_empty": True,
        "object_alternates_absent": True,
    }


def extract_patch(work, base_commit="HEAD"):
    # Use a private index: include new solution files without mutating the solver's index.
    # Only reserved harness directories are excluded; normal source/test additions remain.
    env = {**os.environ, "GIT_INDEX_FILE": str(work.parent / "patch.index")}
    run(["git", "read-tree", base_commit], cwd=work, env=env)
    run(
        ["git", "add", "-A", "--", ".", ":(exclude).run", ":(exclude).agents", ":(exclude).env"],
        cwd=work,
        env=env,
    )
    return run(["git", "diff", "--cached", "--binary", base_commit], cwd=work, env=env)


def log(campaign, value):
    with LOG_LOCK:
        with (campaign / "progress.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"at": time.time(), **value}) + "\n")
        print(json.dumps(value), flush=True)


def solve_trial(campaign, config, sample, task):
    trial = campaign / "trials" / f"sample-{sample}" / task["instance_id"]
    if (trial / "result.json").exists():
        return read(trial / "result.json")
    # Never retry an uncertain paid request automatically: a previous marker is terminal.
    if (trial / "started.json").exists():
        outcome = {
            "status": "interrupted",
            "reason": "previous attempt has no durable result",
            "patch_bytes": 0,
            "cost_usd": None,
        }
        if (trial / "work/.git").exists():
            patch = extract_patch(trial / "work", task["base_commit"])
            (trial / "solution.patch").write_text(patch, encoding="utf-8", newline="")
            outcome["patch_bytes"] = len(patch.encode("utf-8"))
            outcome["patch_sha256"] = digest(trial / "solution.patch")
        write(trial / "result.json", outcome)
        log(campaign, {"sample": sample, "instance_id": task["instance_id"], **outcome})
        return outcome
    trial.mkdir(parents=True, exist_ok=False)
    write(trial / "input.json", task)
    write(trial / "started.json", {"at": time.time(), "pid": os.getpid()})
    started = time.monotonic()
    env = environment(config, trial)
    outcome = {"status": "error", "cost_usd": None}
    try:
        repo_name = task["repo"].split("/")[-1]
        repo = Path(config["repos"]) / repo_name
        prepare_repository(repo, trial / "work", task["base_commit"])
        prepare_tool_environment(config, trial)
        process = subprocess.Popen(
            [
                config["python"],
                str(campaign / "frozen/runner.py"),
                "child",
                "--campaign",
                str(campaign),
                "--trial",
                str(trial),
            ],
            cwd=trial / "work",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        write(trial / "process.json", {"pid": process.pid, "started": time.time()})
        try:
            stdout, _ = process.communicate(
                timeout=config["solve_timeout"] + config["review_timeout"]
            )
            outcome["status"] = "completed" if process.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            # Kill this new trial's process tree, not the gateway or other campaign tasks.
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True
                )
            else:
                process.kill()
            stdout, _ = process.communicate(timeout=30)
            outcome["status"] = "timeout"
        (trial / "process.log").write_text(
            redact(stdout.decode("utf-8", errors="replace"), env), encoding="utf-8"
        )
        outcome["exit_code"] = process.returncode
        if (trial / "solve-timeout.json").exists():
            outcome["status"] = "timeout"
        patch = extract_patch(trial / "work", task["base_commit"])
        (trial / "solution.patch").write_text(patch, encoding="utf-8", newline="")
        outcome["patch_bytes"] = len(patch.encode("utf-8"))
        outcome["patch_sha256"] = digest(trial / "solution.patch")
    except Exception as exc:
        outcome["error"] = redact(f"{type(exc).__name__}: {exc}", env)
    outcome["elapsed_seconds"] = time.monotonic() - started
    write(trial / "result.json", outcome)
    log(campaign, {"sample": sample, "instance_id": task["instance_id"], **outcome})
    return outcome


def safe_solve_trial(campaign, config, sample, task):
    try:
        return solve_trial(campaign, config, sample, task)
    except Exception as exc:
        outcome = {
            "status": "error",
            "error": redact(str(exc), environment(config)),
            "cost_usd": None,
            "patch_bytes": 0,
        }
        trial = campaign / "trials" / f"sample-{sample}" / task["instance_id"]
        write(trial / "result.json", outcome)
        log(campaign, {"sample": sample, "instance_id": task["instance_id"], **outcome})
        return outcome


def solve(campaign):
    config = verify_freeze(campaign)
    if not read(campaign / "preflight-latest.json")["ready"]:
        raise RuntimeError("A successful preflight is required")
    # The OS releases this lock on crash; do not mistake an active paid solve for interruption.
    import msvcrt

    with (campaign / "solve.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        for process_file in (campaign / "trials").glob("sample-*/*/process.json"):
            if (process_file.parent / "result.json").exists():
                continue
            pid = read(process_file)["pid"]
            listing = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
            if f'"{pid}"' in listing:
                raise RuntimeError(f"Previous trial process {pid} still exists; wait before resume")
        with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
            futures = [
                pool.submit(safe_solve_trial, campaign, config, sample, task)
                for sample, task in trials(read(campaign / "tasks.json"), config["samples"])
            ]
            for future in futures:
                future.result()
    return status(campaign)


def reduce_scores(ids, resolved_sets):
    counts = [sum(instance in resolved for resolved in resolved_sets) for instance in ids]
    k = len(resolved_sets)
    n = len(ids)
    return {
        "tasks": n,
        "samples": k,
        "total_trials": n * k,
        "pass_at_1": sum(counts) / (n * k),
        "pass_at_3": sum(c > 0 for c in counts) / n,
        "pass_cubed": sum(c == k for c in counts) / n,
        "resolved_per_sample": [len(set(ids) & resolved) for resolved in resolved_sets],
    }


def grade(campaign):
    config = verify_freeze(campaign)
    tasks = read(campaign / "tasks.json")
    if status(campaign)["finished"] != config["total_trials"]:
        raise RuntimeError("All trial outcomes must be durable before official grading")
    grading = campaign / "grading"
    grading.mkdir(exist_ok=True)
    for sample in range(1, config["samples"] + 1):
        destination = grading / f"sample-{sample}"
        destination.mkdir(exist_ok=True)
        if (destination / "grade-result.json").exists():
            continue
        predictions = destination / "predictions.jsonl"
        rows = []
        for task in tasks:
            trial = campaign / "trials" / f"sample-{sample}" / task["instance_id"]
            patch = trial / "solution.patch"
            rows.append(
                {
                    "instance_id": task["instance_id"],
                    "model_name_or_path": config["model"],
                    "model_patch": patch.read_text(encoding="utf-8") if patch.exists() else "",
                }
            )
        if not predictions.exists():
            predictions.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
        run_id = f"{config['campaign_id']}-sample-{sample}"
        command = [
            config["grader_python"],
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            config["dataset"],
            "--predictions_path",
            str(predictions),
            "--run_id",
            run_id,
            "--max_workers",
            "4",
            "--instance_ids",
            *[task["instance_id"] for task in tasks],
        ]
        write(destination / "command.json", command)
        with (destination / f"grader-{time.time_ns()}.log").open("w", encoding="utf-8") as output:
            result = subprocess.run(
                command,
                cwd=destination,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                timeout=21600,
            )
        reports = list(destination.glob(f"*.{run_id}.json"))
        outcome = {
            "exit_code": result.returncode,
            "report": str(reports[0]) if len(reports) == 1 else None,
            "predictions_sha256": digest(predictions),
        }
        if result.returncode == 0 and len(reports) == 1:
            write(destination / "grade-result.json", outcome)
            write(destination / "artifact-hashes.json", inventory(destination))
        else:
            write(destination / f"grade-error-{time.time_ns()}.json", outcome)
    return status(campaign)


def status(campaign):
    config = read(campaign / "config.json")
    tasks = read(campaign / "tasks.json")
    counts = {}
    for sample, task in trials(tasks, config["samples"]):
        result = campaign / "trials" / f"sample-{sample}" / task["instance_id"] / "result.json"
        state = (
            read(result)["status"]
            if result.exists()
            else "running"
            if (result.parent / "started.json").exists()
            else "pending"
        )
        counts[state] = counts.get(state, 0) + 1
    sets = []
    for sample in range(1, config["samples"] + 1):
        receipt = campaign / "grading" / f"sample-{sample}" / "grade-result.json"
        if receipt.exists():
            report = read(read(receipt)["report"])
            sets.append(set(report["resolved_ids"]))
    report = {
        "campaign": str(campaign),
        "total": config["total_trials"],
        "counts": counts,
        "finished": config["total_trials"] - counts.get("pending", 0) - counts.get("running", 0),
        "graded_samples": len(sets),
        "scores": reduce_scores([t["instance_id"] for t in tasks], sets)
        if len(sets) == config["samples"]
        else None,
    }
    write(campaign / "status.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["prepare", "preflight", "solve", "grade", "status", "child"]
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / ".run/swebench/all50.json")
    parser.add_argument("--grader-python", type=Path, default=Path("E:/Anaconda/python.exe"))
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--samples", type=int, choices=[3], default=3)
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--solve-timeout", type=int, default=5400)
    parser.add_argument("--review-timeout", type=int, default=300)
    parser.add_argument("--trial", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    args.campaign = args.campaign.resolve()
    if args.stage == "child":
        asyncio.run(application_child(args.campaign, args.trial, probe=args.probe))
        return
    result = prepare(args) if args.stage == "prepare" else globals()[args.stage](args.campaign)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
