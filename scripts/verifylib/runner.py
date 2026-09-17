"""Run verification steps in order and stop at the first failure.

Steps execute serially and stream their raw output. Serial execution is deliberate:
the suite shares one state directory, so parallel steps would
race on real durable state rather than on isolated fixtures.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Step:
    """One command in a verification plan."""

    name: str
    argv: tuple[str, ...]
    note: str = ""

    def display(self) -> str:
        return " ".join(self.argv)


@dataclass(frozen=True)
class Plan:
    """An ordered list of steps plus the caveats the caller must see."""

    steps: tuple[Step, ...]
    notes: tuple[str, ...] = field(default=())


def run_step(step: Step, cwd: Path) -> int:
    """Run one step with its output streamed; return its exit code."""
    print(f"\n=== {step.name} ===\n$ {step.display()}", flush=True)
    if step.note:
        print(f"    note: {step.note}", flush=True)
    started = time.perf_counter()
    completed = subprocess.run(step.argv, cwd=cwd, text=True, encoding="utf-8", errors="replace")
    elapsed = time.perf_counter() - started
    print(f"--- {step.name}: exit {completed.returncode} in {elapsed:.1f}s", flush=True)
    return completed.returncode


def run_plan(plan: Plan, cwd: Path) -> int:
    """Run every step in order; return non-zero at the first failure."""
    for note in plan.notes:
        print(f"note: {note}", flush=True)
    for step in plan.steps:
        if run_step(step, cwd) != 0:
            print(f"\nFAILED at step '{step.name}'. Later steps were not run.", flush=True)
            return 1
    print(f"\nAll {len(plan.steps)} steps passed.", flush=True)
    return 0
