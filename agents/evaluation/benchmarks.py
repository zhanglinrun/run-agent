"""Adapters and a live runner for the checked-in GAIA/HLE subsets.

The runner intentionally keeps model execution separate from verification.  A
run first produces predictions and traces; scoring is deterministic and can be
repeated later without another API call.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from typing import Any
import unicodedata

from dotenv import find_dotenv, load_dotenv

from ..app import Agent
from ..providers.config import resolve_api_config
from ..runtime.contracts import utc_now
from ..tools import tool_definitions
from ..runtime.tracing import trace_digest, load_trace


@dataclass
class BenchmarkCase:
    case_id: str
    prompt: str
    answer: str
    problem_type: str
    source: str
    metadata: dict[str, Any]
    attachment: str | None = None


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON array: {path}")
    return [item for item in value if isinstance(item, dict)]


def load_gaia(path: str | Path = "data/GAIA/all.json") -> list[BenchmarkCase]:
    dataset = Path(path)
    root = dataset.parent / "files"
    cases = []
    for item in _load_json_list(dataset):
        attachment = str(root / item["file_name"]) if item.get("file_name") else None
        question = str(item.get("Question") or "").strip()
        if attachment:
            question += f"\n\nAttachment: {Path(attachment).resolve()}"
        question += "\n\nReturn the final answer on the last line as `FINAL ANSWER: <answer>`."
        cases.append(
            BenchmarkCase(
                case_id=str(item.get("task_id") or item.get("id")),
                prompt=question,
                answer=str(item.get("answer") or ""),
                problem_type=str(item.get("problem_type") or "unknown"),
                source="GAIA",
                metadata={"level": item.get("Level"), "row_id": item.get("id")},
                attachment=attachment,
            )
        )
    return cases


def load_hle(path: str | Path = "data/HLE/all_500.json") -> list[BenchmarkCase]:
    dataset = Path(path)
    root = dataset.parent / "images"
    cases = []
    for item in _load_json_list(dataset):
        image_name = str(item.get("image") or "").strip()
        attachment = str(root / image_name) if image_name else None
        question = str(item.get("question") or "").strip()
        if attachment:
            question += f"\n\nImage attachment: {Path(attachment).resolve()}"
        question += "\n\nReturn the final answer on the last line as `FINAL ANSWER: <answer>`."
        cases.append(
            BenchmarkCase(
                case_id=str(item.get("id") or ""),
                prompt=question,
                answer=str(item.get("answer") or ""),
                problem_type=str(item.get("problem_type") or "unknown"),
                source="HLE",
                metadata={
                    "answer_type": item.get("answer_type"),
                    "category": item.get("category"),
                    "subject": item.get("raw_subject"),
                },
                attachment=attachment,
            )
        )
    return cases


def load_benchmark(name: str, path: str | None = None) -> tuple[Path, list[BenchmarkCase]]:
    normalized = name.lower()
    if normalized == "gaia":
        source = Path(path or "data/GAIA/all.json")
        return source, load_gaia(source)
    if normalized == "hle":
        source = Path(path or "data/HLE/all_500.json")
        return source, load_hle(source)
    raise ValueError(f"unsupported benchmark: {name}; choose gaia or hle")


def extract_final_answer(text: str) -> str:
    matches = re.findall(r"(?im)^\s*FINAL\s+ANSWER\s*:\s*(.+?)\s*$", str(text or ""))
    if matches:
        return matches[-1].strip().strip("`*")
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-1].strip("`*") if lines else ""


def normalize_answer(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"^['\"]|['\"]$", "", text)
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(".")
    if re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?", text):
        text = text.replace(",", "")
    return text


def exact_match(prediction: str, reference: str) -> bool:
    return normalize_answer(prediction) == normalize_answer(reference)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    status = run("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _runtime_metadata(root: Path) -> dict[str, Any]:
    versions: dict[str, str] = {}
    for package in ("openai", "anthropic", "python-dotenv", "rich"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    settings_hashes: dict[str, str] = {}
    for path in (Path.home() / ".run" / "settings.json", root / ".run" / "settings.json", root / ".mcp.json"):
        if path.exists():
            settings_hashes[str(path)] = _sha256(path)
    policy_files = sorted((root / "agents" / "policy").glob("*.py"))
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": versions,
        "tool_schema_sha256": _json_sha256(tool_definitions),
        "policy_sha256": _json_sha256({str(path.relative_to(root)): _sha256(path) for path in policy_files}),
        "settings_sha256": settings_hashes,
    }


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def _selected_cases(
    cases: list[BenchmarkCase],
    *,
    problem_type: str | None,
    case_ids: set[str],
    limit: int | None,
    seed: int,
) -> list[BenchmarkCase]:
    selected = [case for case in cases if not problem_type or case.problem_type == problem_type]
    if case_ids:
        selected = [case for case in selected if case.case_id in case_ids]
    rng = random.Random(seed)
    rng.shuffle(selected)
    return selected[:limit] if limit else selected


async def run_live(args: argparse.Namespace) -> Path:
    dataset_path, cases = load_benchmark(args.benchmark, args.dataset)
    if args.problem_type == "mm" and not args.allow_unsupported_mm:
        raise ValueError(
            "multimodal cases require an image-input adapter; pass --allow-unsupported-mm only to record them as unsupported failures"
        )
    excluded_unsupported_mm = 0
    selectable_cases = cases
    if args.problem_type is None and not args.allow_unsupported_mm:
        excluded_unsupported_mm = sum(1 for case in cases if case.problem_type == "mm")
        selectable_cases = [case for case in cases if case.problem_type != "mm"]
    selected = _selected_cases(
        selectable_cases,
        problem_type=args.problem_type,
        case_ids=set(args.case_id or []),
        limit=args.limit,
        seed=args.seed,
    )
    if not selected:
        raise ValueError("no benchmark cases matched the selection")
    unsupported_mm = [case.case_id for case in selected if case.problem_type == "mm"]
    if unsupported_mm and not args.allow_unsupported_mm:
        raise ValueError(
            f"selected {len(unsupported_mm)} multimodal cases, but this Runtime has no image-input adapter; "
            "use --problem-type text or explicitly pass --allow-unsupported-mm to record them as unsupported"
        )

    base_url, api_key, use_openai = resolve_api_config(cli_api_base=args.api_base)
    if not api_key:
        raise RuntimeError("API key is required for a live benchmark run")
    model = args.model or os.environ.get("MODEL") or "deepseek-chat"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = Path(args.output) / f"{args.benchmark}-{stamp}-{model.replace('/', '_')}"
    trace_dir = run_dir / "traces"
    predictions_path = run_dir / "predictions.jsonl"
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "benchmark": args.benchmark,
        "dataset": str(dataset_path.resolve()),
        "dataset_sha256": _sha256(dataset_path),
        "model": model,
        "protocol": "openai" if use_openai else "anthropic",
        "base_url_host": re.sub(r"(?i)^(https?://[^/]+).*$", r"\1", base_url or "default"),
        "seed": args.seed,
        "temperature": args.temperature,
        "fold_enabled": not args.disable_fold,
        "run_config": {
            "permission_mode": args.permission_mode,
            "max_turns": args.max_turns,
            "max_cost_usd": args.max_cost,
            "thinking": args.thinking,
            "memory_enabled": args.with_memory,
            "skills_enabled": args.with_skills,
            "skill_evolution_enabled": False,
            "runtime_verification_enabled": True,
        },
        "git": _git_metadata(Path.cwd()),
        "runtime": _runtime_metadata(Path.cwd()),
        "selection": {
            "limit": args.limit,
            "problem_type": args.problem_type,
            "case_ids": args.case_id or [],
            "selected_count": len(selected),
            "dataset_count": len(cases),
            "excluded_unsupported_mm": excluded_unsupported_mm,
        },
        "started_at": utc_now(),
        "status": "running",
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    passed = 0
    errors = 0
    total_input = 0
    total_output = 0
    try:
        for index, case in enumerate(selected, start=1):
            started = time.perf_counter()
            disabled_extensions = {"skill-evolution"}
            if not args.with_memory:
                disabled_extensions.add("memory")
            if not args.with_skills:
                disabled_extensions.add("skills")
            if args.disable_fold:
                disabled_extensions.add("context")
            agent = Agent(
                permission_mode=args.permission_mode,
                model=model,
                thinking=args.thinking,
                max_cost_usd=args.max_cost,
                max_turns=args.max_turns,
                api_base=base_url,
                api_key=api_key,
                use_openai=use_openai,
                trace_root=trace_dir,
                trace_metadata={
                    "case_id": case.case_id,
                    "benchmark": case.source,
                    "problem_type": case.problem_type,
                },
                persist_session=False,
                disable_extensions=tuple(sorted(disabled_extensions)),
                temperature=args.temperature,
            )
            error = None
            task_result = None
            try:
                task_result = await agent.run_once(case.prompt)
            except Exception as exc:  # failure is evidence, not a reason to lose the campaign
                error = f"{type(exc).__name__}: {exc}"
                errors += 1
            finally:
                await agent.close()

            prediction = extract_final_answer(task_result.answer if task_result is not None else "")
            unsupported = case.problem_type == "mm"
            if unsupported:
                error = error or "unsupported_multimodal_input"
                errors += int(error == "unsupported_multimodal_input")
            correct = exact_match(prediction, case.answer) if not error and not unsupported else False
            passed += int(correct)
            tokens = task_result.usage.to_dict() if task_result is not None else {"input": 0, "output": 0}
            total_input += int(tokens.get("input", 0) or 0)
            total_output += int(tokens.get("output", 0) or 0)
            trace_path = task_result.trace_path if task_result is not None else None
            trace_sha256 = trace_digest(load_trace(trace_path)) if trace_path and trace_path.exists() else None
            row = {
                "index": index,
                "case": asdict(case),
                "prediction": prediction,
                "reference": case.answer,
                "correct": correct,
                "error": error,
                "tokens": tokens,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "trace": str(trace_path) if trace_path else None,
                "trace_sha256": trace_sha256,
                "unsupported": unsupported,
            }
            _append_jsonl(predictions_path, row)
            print(f"[{index}/{len(selected)}] {case.case_id} {'PASS' if correct else 'FAIL'}")
    finally:
        manifest["completed_at"] = utc_now()
        manifest["status"] = "completed"
        manifest["summary"] = {
            "cases": len(selected),
            "passed": passed,
            "failed": len(selected) - passed,
            "errors": errors,
            "pass_at_1": round(passed / len(selected), 6),
            "tokens": {"input": total_input, "output": total_output},
        }
        manifest["predictions"] = str(predictions_path)
        if predictions_path.exists():
            manifest["predictions_sha256"] = _sha256(predictions_path)
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-agent-benchmark")
    parser.add_argument("benchmark", choices=["gaia", "hle"])
    parser.add_argument("--dataset", help="Override dataset JSON path")
    parser.add_argument("--model")
    parser.add_argument("--api-base")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--problem-type", choices=["text", "file", "mm"])
    parser.add_argument("--output", default=".run/benchmark-runs")
    parser.add_argument("--permission-mode", default="dontAsk", choices=["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"])
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--disable-fold", action="store_true", help="Disable structured context folding for an ablation arm")
    parser.add_argument("--with-memory", action="store_true", help="Enable long-term memory retrieval (off by default for isolation)")
    parser.add_argument("--with-skills", action="store_true", help="Enable project/user skills (off by default for isolation)")
    parser.add_argument(
        "--allow-unsupported-mm",
        action="store_true",
        help="Include GAIA/HLE multimodal cases as explicit unsupported failures; does not claim multimodal support",
    )
    return parser


def main() -> None:
    env_path = find_dotenv(usecwd=True)
    load_dotenv(env_path or None, override=False)
    args = build_parser().parse_args()
    run_dir = asyncio.run(run_live(args))
    print(f"run_dir={run_dir}")


if __name__ == "__main__":
    main()
