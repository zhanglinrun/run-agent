"""Dataset loading, trace matching and reproducible report generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..runtime.contracts import EvalCase, utc_now
from ..runtime.tracing import load_trace, trace_digest
from .verifiers import verify_trace


def load_cases(path: str | Path) -> list[EvalCase]:
    source = Path(path)
    if source.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        raw = json.loads(source.read_text(encoding="utf-8"))
        rows = raw.get("cases", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("evaluation dataset must be a JSON array or JSONL objects")
    cases = [EvalCase.from_dict(row, index) for index, row in enumerate(rows) if isinstance(row, dict)]
    if not cases:
        raise ValueError(f"no evaluation cases found in {source}")
    return cases


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_trace(trace_dir: Path, case: EvalCase) -> Path:
    direct = trace_dir / f"{case.case_id}.jsonl"
    if direct.exists():
        return direct
    for path in sorted(trace_dir.glob("*.jsonl")):
        events = load_trace(path)
        started = events[0].get("payload", {}) if events else {}
        if isinstance(started, dict) and str(started.get("case_id") or "") == case.case_id:
            return path
    raise FileNotFoundError(f"no trace found for case {case.case_id!r} under {trace_dir}")


class EvaluationRunner:
    def __init__(self, *, output_root: str | Path = ".run/evals") -> None:
        self.output_root = Path(output_root)

    def replay(self, dataset: str | Path, trace_dir: str | Path) -> dict[str, Any]:
        dataset_path = Path(dataset)
        traces_path = Path(trace_dir)
        cases = load_cases(dataset_path)
        results = []
        evidence = []
        for case in cases:
            trace_path = _find_trace(traces_path, case)
            events = load_trace(trace_path)
            result = verify_trace(case, events)
            results.append(result.to_dict())
            evidence.append(
                {
                    "case_id": case.case_id,
                    "trace": str(trace_path),
                    "trace_sha256": _sha256_file(trace_path),
                    "normalized_trace_sha256": trace_digest(events),
                }
            )

        passed = sum(1 for item in results if item["passed"])
        report = {
            "schema_version": 1,
            "mode": "replay",
            "created_at": utc_now(),
            "dataset": str(dataset_path),
            "dataset_sha256": _sha256_file(dataset_path),
            "trace_dir": str(traces_path),
            "summary": {
                "cases": len(results),
                "passed": passed,
                "failed": len(results) - passed,
                "pass_rate": round(passed / len(results), 6),
                "mean_score": round(sum(float(item["score"]) for item in results) / len(results), 6),
                "mean_correctness": round(sum(float(item["correctness"]) for item in results) / len(results), 6),
                "mean_process": round(sum(float(item["process"]) for item in results) / len(results), 6),
                "mean_safety": round(sum(float(item["safety"]) for item in results) / len(results), 6),
            },
            "results": results,
            "evidence": evidence,
        }
        return report

    def save_report(self, report: dict[str, Any], *, name: str = "latest") -> Path:
        self.output_root.mkdir(parents=True, exist_ok=True)
        path = self.output_root / f"{name}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def format_report(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        "Run Agent evaluation",
        f"cases={summary.get('cases', 0)} passed={summary.get('passed', 0)} failed={summary.get('failed', 0)}",
        f"pass_rate={float(summary.get('pass_rate', 0)):.1%} mean_score={float(summary.get('mean_score', 0)):.3f}",
    ]
    failures = [item for item in report.get("results", []) if not item.get("passed")]
    for item in failures[:10]:
        failed_checks = [check.get("name") for check in item.get("checks", []) if not check.get("passed")]
        lines.append(f"FAIL {item.get('case_id')}: {', '.join(str(name) for name in failed_checks)}")
    return "\n".join(lines)
