"""Official SWE-bench harness invocation and report projection."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .adapter import DATASET_ID, DATASET_SPLIT
from .models import SWEBenchInstance


def official_case_status(report: dict[str, Any], instance_id: str) -> dict[str, Any]:
    list_fields = {
        "resolved_ids": "resolved",
        "unresolved_ids": "unresolved",
        "error_ids": "error",
        "empty_patch_ids": "empty_patch",
        "completed_ids": "completed",
    }
    for field, status in list_fields.items():
        values = report.get(field)
        if isinstance(values, list) and instance_id in {str(item) for item in values}:
            return {"status": status, "instance_id": instance_id, "source_field": field}
    for field in ("instances", "results", "instance_results"):
        values = report.get(field)
        if isinstance(values, dict) and instance_id in values:
            return {"status": "reported", "instance_id": instance_id, "result": values[instance_id]}
    return {"status": "grader_completed", "instance_id": instance_id}


def run_official_grader(
    predictions_path: Path,
    instances: list[SWEBenchInstance],
    run_dir: Path,
    args,
) -> Path | None:
    if importlib.util.find_spec("swebench") is None:
        raise RuntimeError("official grader is not installed; install run-agent-harness[swebench]")
    command = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", DATASET_ID,
        "--split", DATASET_SPLIT,
        "--predictions_path", str(predictions_path),
        "--run_id", run_dir.name,
        "--report_dir", str(run_dir / "official-report"),
        "--max_workers", str(args.max_workers),
        "--timeout", str(args.eval_timeout),
        "--instance_ids", *[item.instance_id for item in instances],
    ]
    env = os.environ.copy()
    if sys.platform == "win32":
        # The upstream grader writes eval.sh with the host platform newline
        # convention.  Add our compatibility module to the subprocess path so
        # bash receives LF-only scripts inside the Linux evaluator container.
        compat_root = str(Path(__file__).resolve().parent)
        env["PYTHONPATH"] = os.pathsep.join(
            [compat_root, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [compat_root]
        )
    result = subprocess.run(
        command, cwd=str(run_dir), text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False, env=env,
    )
    (run_dir / "official-grader.command.json").write_text(
        json.dumps({"argv": command}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "official-grader.stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_dir / "official-grader.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"official SWE-bench grader failed with exit code {result.returncode}")
    reports = sorted((run_dir / "official-report").glob("*.json"))
    return reports[0] if reports else None


__all__ = ["official_case_status", "run_official_grader"]
