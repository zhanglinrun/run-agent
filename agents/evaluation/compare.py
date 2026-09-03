"""Paired comparison for two benchmark prediction files."""

from __future__ import annotations

import argparse
from math import comb
import json
from pathlib import Path
import random
from typing import Any

from ..runtime.contracts import utc_now


def _load_adjacent_manifest(predictions_path: str | Path) -> dict[str, Any] | None:
    path = Path(predictions_path).resolve().parent / "manifest.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _comparison_config(manifest: dict[str, Any]) -> dict[str, Any]:
    run_config = manifest.get("run_config") if isinstance(manifest.get("run_config"), dict) else {}
    selection = manifest.get("selection") if isinstance(manifest.get("selection"), dict) else {}
    return {
        "benchmark": manifest.get("benchmark"),
        "dataset_sha256": manifest.get("dataset_sha256"),
        "model": manifest.get("model"),
        "protocol": manifest.get("protocol"),
        "base_url_host": manifest.get("base_url_host", manifest.get("provider_base_url_host")),
        "seed": manifest.get("seed"),
        "temperature": manifest.get("temperature", run_config.get("temperature")),
        "fold_enabled": manifest.get("fold_enabled", run_config.get("context_compaction_enabled")),
        "selected_instance_ids": manifest.get("selected_instance_ids"),
        "tool_schema_sha256": manifest.get("tool_schema_sha256"),
        "policy_sha256": manifest.get("policy_sha256"),
        "sandbox_images": manifest.get("sandbox_images"),
        "selection.problem_type": selection.get("problem_type"),
        "selection.case_ids": selection.get("case_ids"),
        "permission_mode": run_config.get("permission_mode"),
        "max_turns": run_config.get("max_turns"),
        "max_cost_usd": run_config.get("max_cost_usd", run_config.get("max_cost_usd_per_task")),
        "thinking": run_config.get("thinking"),
        "memory_enabled": run_config.get("memory_enabled"),
        "skills_enabled": run_config.get("skills_enabled"),
        "harness_mode": run_config.get("harness_mode"),
        "runtime_verification_enabled": run_config.get("runtime_verification_enabled"),
        "correction_enabled": run_config.get("correction_enabled"),
        "max_repair_attempts": run_config.get("max_repair_attempts"),
        "execution_backend": run_config.get("execution_backend"),
        "network": run_config.get("network"),
        "memory_mb": run_config.get("memory_mb"),
        "cpus": run_config.get("cpus"),
        "pids_limit": run_config.get("pids_limit"),
    }


def load_predictions(path: str | Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case = row.get("case") if isinstance(row.get("case"), dict) else {}
        case_id = str(case.get("case_id") or row.get("case_id") or row.get("instance_id") or "")
        if not case_id:
            raise ValueError(f"prediction row has no case id: {path}")
        if case_id in rows:
            raise ValueError(f"duplicate case id {case_id!r}: {path}")
        rows[case_id] = row
    if not rows:
        raise ValueError(f"no predictions found: {path}")
    return rows


def mcnemar_exact_p_value(baseline_only: int, candidate_only: int) -> float:
    """Two-sided exact binomial p-value for discordant paired outcomes."""

    n = baseline_only + candidate_only
    if n == 0:
        return 1.0
    k = min(baseline_only, candidate_only)
    lower_tail = sum(comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2.0 * lower_tail)


def paired_bootstrap_ci(deltas: list[float], *, samples: int = 10_000, seed: int = 42) -> tuple[float, float]:
    if not deltas:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(max(100, samples)):
        means.append(sum(deltas[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    low_index = int(0.025 * (len(means) - 1))
    high_index = int(0.975 * (len(means) - 1))
    return means[low_index], means[high_index]


def compare_predictions(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    bootstrap_samples: int = 10_000,
    seed: int = 42,
    allowed_manifest_differences: set[str] | None = None,
) -> dict[str, Any]:
    baseline = load_predictions(baseline_path)
    candidate = load_predictions(candidate_path)
    if set(baseline) != set(candidate):
        missing_candidate = sorted(set(baseline) - set(candidate))
        missing_baseline = sorted(set(candidate) - set(baseline))
        raise ValueError(
            "paired comparison requires identical case ids; "
            f"missing_candidate={missing_candidate[:10]} missing_baseline={missing_baseline[:10]}"
        )

    baseline_manifest = _load_adjacent_manifest(baseline_path)
    candidate_manifest = _load_adjacent_manifest(candidate_path)
    config_differences: dict[str, dict[str, Any]] = {}
    if baseline_manifest and candidate_manifest:
        base_config = _comparison_config(baseline_manifest)
        candidate_config = _comparison_config(candidate_manifest)
        config_differences = {
            key: {"baseline": base_config.get(key), "candidate": candidate_config.get(key)}
            for key in sorted(set(base_config) | set(candidate_config))
            if base_config.get(key) != candidate_config.get(key)
        }
        allowed = set(allowed_manifest_differences or set())
        unexpected = sorted(set(config_differences) - allowed)
        if unexpected:
            raise ValueError(
                "benchmark manifests differ outside the declared ablation axes: "
                + ", ".join(unexpected)
            )

    case_ids = sorted(baseline)
    paired_rows = []
    baseline_only = 0
    candidate_only = 0
    both_correct = 0
    both_wrong = 0
    correctness_deltas: list[float] = []
    input_token_deltas: list[float] = []
    output_token_deltas: list[float] = []
    for case_id in case_ids:
        base = baseline[case_id]
        cand = candidate[case_id]
        if "correct" not in base or "correct" not in cand:
            raise ValueError(
                f"case {case_id} has no scored correctness; run the official grader before comparing SWE-bench arms"
            )
        if str(base.get("reference") or "") != str(cand.get("reference") or ""):
            raise ValueError(f"reference mismatch for case {case_id}")
        base_correct = bool(base.get("correct"))
        cand_correct = bool(cand.get("correct"))
        if base_correct and cand_correct:
            both_correct += 1
        elif base_correct:
            baseline_only += 1
        elif cand_correct:
            candidate_only += 1
        else:
            both_wrong += 1
        correctness_deltas.append(float(cand_correct) - float(base_correct))
        base_tokens = base.get("tokens") if isinstance(base.get("tokens"), dict) else {}
        cand_tokens = cand.get("tokens") if isinstance(cand.get("tokens"), dict) else {}
        input_token_deltas.append(float(cand_tokens.get("input", 0) or 0) - float(base_tokens.get("input", 0) or 0))
        output_token_deltas.append(float(cand_tokens.get("output", 0) or 0) - float(base_tokens.get("output", 0) or 0))
        paired_rows.append(
            {
                "case_id": case_id,
                "baseline_correct": base_correct,
                "candidate_correct": cand_correct,
                "correctness_delta": int(cand_correct) - int(base_correct),
            }
        )

    n = len(case_ids)
    baseline_pass = sum(int(bool(row.get("correct"))) for row in baseline.values()) / n
    candidate_pass = sum(int(bool(row.get("correct"))) for row in candidate.values()) / n
    ci_low, ci_high = paired_bootstrap_ci(correctness_deltas, samples=bootstrap_samples, seed=seed)
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "baseline": str(Path(baseline_path)),
        "candidate": str(Path(candidate_path)),
        "cases": n,
        "pass_at_1": {
            "baseline": round(baseline_pass, 6),
            "candidate": round(candidate_pass, 6),
            "absolute_delta": round(candidate_pass - baseline_pass, 6),
            "paired_bootstrap_95_ci": [round(ci_low, 6), round(ci_high, 6)],
        },
        "mcnemar": {
            "both_correct": both_correct,
            "baseline_only": baseline_only,
            "candidate_only": candidate_only,
            "both_wrong": both_wrong,
            "exact_two_sided_p": round(mcnemar_exact_p_value(baseline_only, candidate_only), 8),
        },
        "mean_token_delta": {
            "input": round(sum(input_token_deltas) / n, 3),
            "output": round(sum(output_token_deltas) / n, 3),
        },
        "paired_results": paired_rows,
        "bootstrap": {"samples": max(100, bootstrap_samples), "seed": seed},
        "evidence": {
            "baseline_manifest": bool(baseline_manifest),
            "candidate_manifest": bool(candidate_manifest),
            "allowed_manifest_differences": sorted(allowed_manifest_differences or set()),
            "configuration_differences": config_differences,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-agent-compare", description="Paired comparison of benchmark runs")
    parser.add_argument("baseline", help="Baseline predictions.jsonl")
    parser.add_argument("candidate", help="Candidate predictions.jsonl")
    parser.add_argument("--output", default="comparison.json")
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--allow-difference",
        action="append",
        default=[],
        help="Declare an intentional manifest difference, e.g. fold_enabled or skills_enabled",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = compare_predictions(
        args.baseline,
        args.candidate,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
        allowed_manifest_differences=set(args.allow_difference),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    delta = report["pass_at_1"]["absolute_delta"]
    ci = report["pass_at_1"]["paired_bootstrap_95_ci"]
    p_value = report["mcnemar"]["exact_two_sided_p"]
    print(f"cases={report['cases']} delta={delta:+.1%} bootstrap95=[{ci[0]:+.1%}, {ci[1]:+.1%}] mcnemar_p={p_value}")
    print(f"report={output}")


if __name__ == "__main__":
    main()
