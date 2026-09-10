"""Single authoritative verification gate for run-agent.

Full mode mirrors ``.github/workflows/ci.yml`` step for step: formatting, lint,
types, the test suite, then wheel/sdist build and a clean-installation check.
``--fast`` runs the changed-file subset for local iteration and prints exactly
what it skipped; it is a convenience, never a gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from verifylib import affected, distcheck, runner  # noqa: E402


def static_steps(python: str, targets: tuple[str, ...]) -> list[runner.Step]:
    """Formatting, lint and type steps over the given targets."""
    return [
        runner.Step("format", (python, "-m", "ruff", "format", "--check", *targets)),
        runner.Step("lint", (python, "-m", "ruff", "check", *targets)),
        runner.Step(
            "types",
            (python, "-m", "mypy"),
            note="incremental via .mypy_cache; delete the cache to force a cold run",
        ),
    ]


def dist_steps() -> list[runner.Step]:
    """Build, clean-install and validate the distribution."""
    return [
        runner.Step("build", distcheck.build_argv(), note="wheel + sdist into .run/verify/dist"),
        runner.Step(
            "install",
            distcheck.install_argv(),
            note="clean venv; cached by wheel sha256, printed as cache hit/miss",
        ),
        runner.Step(
            "dist-check",
            distcheck.validate_argv(),
            note="scripts/validate_distribution.py outside the repository",
        ),
    ]


def full_plan(python: str, skip_dist: bool) -> runner.Plan:
    """The release-readiness plan; identical to CI plus the local build path."""
    steps = [
        *static_steps(python, (".",)),
        runner.Step("tests", (python, "-m", "pytest", "tests/redesign", "-q")),
    ]
    notes = ["full gate: mirrors .github/workflows/ci.yml step for step"]
    if skip_dist:
        notes.append("--skip-dist: build, install and dist-check were skipped")
    else:
        steps.extend(dist_steps())
    return runner.Plan(tuple(steps), tuple(notes))


def fast_plan(python: str) -> runner.Plan:
    """The changed-file plan; prints what it selected and what it skipped."""
    selection = affected.select(ROOT)
    steps = [
        *static_steps(python, affected.lint_targets(selection.changed)),
        runner.Step("tests", selection.pytest_argv(python)),
    ]
    notes = [
        *selection.notes,
        f"skipped by --fast: build, install, dist-check, and "
        f"{'nothing' if selection.escalated else 'tests outside the selection'}",
        "run without --fast before trusting a change; --fast is not the gate",
    ]
    return runner.Plan(tuple(steps), tuple(notes))


def main() -> int:
    """Run the requested plan and return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="changed-file subset for iteration")
    parser.add_argument("--skip-dist", action="store_true", help="full gate without build steps")
    args = parser.parse_args()
    plan = fast_plan(sys.executable) if args.fast else full_plan(sys.executable, args.skip_dist)
    return runner.run_plan(plan, ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
