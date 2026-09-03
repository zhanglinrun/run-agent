"""Command line entry point for trace replay evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path

from .runner import EvaluationRunner, format_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-agent-eval", description="Replay and verify Run Agent traces")
    parser.add_argument("dataset", help="JSON/JSONL evaluation cases")
    parser.add_argument("--traces", required=True, help="Directory containing one JSONL trace per case")
    parser.add_argument("--output", default=".run/evals", help="Report output directory")
    parser.add_argument("--name", default="latest", help="Report filename without extension")
    parser.add_argument("--fail-on-regression", action="store_true", help="Exit non-zero if any case fails")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = EvaluationRunner(output_root=Path(args.output))
    report = runner.replay(args.dataset, args.traces)
    report_path = runner.save_report(report, name=args.name)
    print(format_report(report))
    print(f"report={report_path}")
    if args.fail_on_regression and report["summary"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
