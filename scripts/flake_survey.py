"""Survey the suite for intermittent tests by running it repeatedly.

The gate failed three times on three different tests, each green in isolation, so
chasing them one at a time answers the wrong question. What is missing is a
distribution: which tests fail, how often, and whether the failing set is stable.

Each pass records the failing test ids and its wall time, and the summary reports per
test failure counts. Failures that name themselves are far cheaper to fix than a gate
that is red once in five runs with no pattern, which is what this repository had.

Usage: python scripts/flake_survey.py [passes] [--timeout-seconds N]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = re.compile(r"^FAILED (\S+)", re.MULTILINE)
SUMMARY = re.compile(r"^(?:(\d+) failed, )?(\d+) passed", re.MULTILINE)


def run_pass(index: int, timeout: float) -> tuple[float, tuple[str, ...], str]:
    """Run the whole suite once and return its duration, failing tests and summary."""
    started = time.monotonic()
    completed = subprocess.run(
        (sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    output = completed.stdout + completed.stderr
    summary = SUMMARY.search(output)
    tail = summary.group(0) if summary else "(no summary line)"
    print(f"  pass {index}: {elapsed:6.1f}s  {tail}", flush=True)
    return elapsed, tuple(FAILED.findall(output)), tail


def cpu_load(stop: threading.Event, workers: int) -> list[threading.Thread]:
    """Saturate the CPU while the suite runs, to raise the reproduction rate.

    The three uncharacterised failures all pass in isolation and cluster under the gate,
    which points at the machine rather than the code. Making the machine busy on purpose
    is how a rare load-sensitive failure is turned into a reproducible one.
    """

    def spin() -> None:
        while not stop.is_set():
            sum(index * index for index in range(10_000))

    threads = [threading.Thread(target=spin, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    return threads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("passes", type=int, nargs="?", default=6)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--load", type=int, default=0, help="CPU-saturating threads to run during each pass"
    )
    args = parser.parse_args()

    failures: Counter[str] = Counter()
    durations: list[float] = []
    print(f"surveying {args.passes} full passes (load={args.load})", flush=True)
    for index in range(1, args.passes + 1):
        stop = threading.Event()
        threads = cpu_load(stop, args.load) if args.load else []
        try:
            elapsed, failed, _ = run_pass(index, args.timeout_seconds)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=1.0)
        durations.append(elapsed)
        failures.update(failed)

    print("\n=== result ===")
    print(f"  passes: {len(durations)}")
    print(f"  wall time: min {min(durations):.1f}s max {max(durations):.1f}s")
    if not failures:
        print("  no failures observed in any pass")
        return 0
    print(f"  distinct failing tests: {len(failures)}")
    for name, count in failures.most_common():
        print(f"    {count}/{len(durations)}  {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
