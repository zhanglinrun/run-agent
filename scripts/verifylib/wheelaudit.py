"""Audit the built wheel layout, mirroring the CI wheel-layout guard.

Required package set matches the CI guard, plus run_agent_entry.py which the
plan requires to ship in the wheel. Stale packages and leaked development
files fail the audit.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REQUIRED = (
    "run_agent_ai/__init__.py",
    "run_agent_core/__init__.py",
    "run_agent_coding/__init__.py",
    "run_agent_gateway/__init__.py",
    "run_agent_observability/__init__.py",
    "run_agent_evals/__init__.py",
    "run_agent_extensions/__init__.py",
    "run_agent_entry.py",
)
LEAK_SUFFIXES = ("/builtins/mem0_memory.py", "/builtins/harness_features.py")


def audit(wheel: Path) -> list[str]:
    """Return every wheel layout problem, empty when the layout is correct."""
    names = set(zipfile.ZipFile(wheel).namelist())
    problems = [f"missing from wheel: {name}" for name in REQUIRED if name not in names]
    problems += [f"stale agents package: {n}" for n in sorted(names) if n.startswith("agents/")]
    problems += [
        f"development file leaked into the wheel: {n}" for n in sorted(names) if _leaked(n)
    ]
    return problems


def _leaked(name: str) -> bool:
    """True when a wheel member should never ship in the core distribution."""
    return name.startswith("extensions/") or name.endswith(LEAK_SUFFIXES)


def main() -> int:
    """Audit the single wheel in --dist and report every problem."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    wheels = sorted(args.dist.glob("*.whl"))
    if len(wheels) != 1:
        print(f"expected exactly one wheel in {args.dist}, found {len(wheels)}", file=sys.stderr)
        return 1
    problems = audit(wheels[0])
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"wheel layout audit failed with {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"wheel layout audit passed: {wheels[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
