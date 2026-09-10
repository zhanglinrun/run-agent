"""Try to reproduce the flake under the condition it was actually seen in.

The three uncharacterised failures appeared while a second Python process of mine was
running mypy plus the suite. CPU spin threads did not reproduce them, and that is
consistent: a rival test process competes for different things than a busy core does -
file handles, the SQLite lock, process-table slots, scheduler latency - and this suite
leans on all four through real subprocesses and a real database.

So the reproduction attempt is the gate with rival suite processes alongside it, not
more arithmetic.

Usage: python scripts/flake_rivals.py [rivals] [passes]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = re.compile(r"^FAILED (\S+)", re.MULTILINE)


def start_rivals(count: int) -> list[subprocess.Popen[str]]:
    """Start rival suite processes that compete for handles, locks and processes."""
    return [
        subprocess.Popen(
            (sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"),
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(count)
    ]


def run_gate(timeout: float) -> tuple[int, tuple[str, ...]]:
    """Run the real gate and return its exit code and failing test ids."""
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts" / "verify.py")),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = completed.stdout + completed.stderr
    return completed.returncode, tuple(FAILED.findall(output))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rivals", type=int, nargs="?", default=2)
    parser.add_argument("passes", type=int, nargs="?", default=4)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()

    failures: Counter[str] = Counter()
    print(f"gate x{args.passes} with {args.rivals} rival suite processes", flush=True)
    for index in range(1, args.passes + 1):
        rivals = start_rivals(args.rivals)
        started = time.monotonic()
        try:
            code, failed = run_gate(args.timeout_seconds)
        finally:
            for rival in rivals:
                rival.terminate()
        elapsed = time.monotonic() - started
        print(f"  pass {index}: gate exit={code} in {elapsed:6.1f}s  failures={list(failed)}")
        failures.update(failed)

    print("\n=== result ===")
    if not failures:
        print(f"  no failures in {args.passes} passes under rivalry")
        return 0
    for name, count in failures.most_common():
        print(f"  {count}/{args.passes}  {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
