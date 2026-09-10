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
    """Build, audit, clean-install, dependency-check and validate the distribution."""
    return [
        runner.Step("build", distcheck.build_argv(), note="wheel + sdist into .run/verify/dist"),
        runner.Step(
            "wheel-audit",
            wheelaudit_argv(),
            note="mirrors the CI wheel layout guard, plus run_agent_entry.py",
        ),
        runner.Step(
            "install",
            distcheck.install_argv(),
            note="clean venv; cached by wheel sha256, printed as cache hit/miss",
        ),
        runner.Step(
            "pip-check",
            distcheck.pip_check_argv(),
            note="dependency check inside the clean environment",
        ),
        runner.Step(
            "dist-check",
            distcheck.validate_argv(),
            note="scripts/validate_distribution.py outside the repository",
        ),
    ]


def wheelaudit_argv() -> tuple[str, ...]:
    """Command that audits the built wheel layout."""
    dist = distcheck.work_paths()[0]
    module = SCRIPTS / "verifylib" / "wheelaudit.py"
    return (sys.executable, str(module), "--dist", str(dist))


def full_plan(python: str, skip_dist: bool) -> runner.Plan:
    """The release-readiness plan; identical to CI plus the local build path."""
    steps = [
        runner.Step(
            "compile",
            (python, "-m", "compileall", "-q", "src", "extensions", "tests"),
            note="mirrors the CI compile step",
        ),
        *static_steps(python, (".",)),
        runner.Step("tests", (python, "-m", "pytest", "-q")),
    ]
    notes = ["full gate: mirrors .github/workflows/ci.yml step for step"]
    if skip_dist:
        notes.append("--skip-dist: build, wheel-audit, install, pip-check, dist-check skipped")
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
    skipped = (
        "no tests (the full suite ran)" if selection.escalated else "tests outside the selection"
    )
    notes = [
        *selection.notes,
        f"skipped by --fast: build, install, dist-check, and {skipped}",
        "run without --fast before trusting a change; --fast is not the gate",
    ]
    return runner.Plan(tuple(steps), tuple(notes))


VERB_ALIASES = {"typecheck": "types", "test": "tests"}
DIST_STEP_NAMES = ("build", "wheel-audit", "install", "pip-check", "dist-check")


def select_only(plan: runner.Plan, names: tuple[str, ...]) -> runner.Plan:
    """Keep only the named steps, pulling in build when a later dist step needs it.

    Verbb names used by the CI hook (typecheck, test) are accepted as aliases of
    the gate's own step names so both spellings resolve to one implementation.
    """
    wanted = {VERB_ALIASES.get(name, name) for name in names}
    if wanted & (set(DIST_STEP_NAMES) - {"build"}):
        wanted.add("build")
    available = [step.name for step in plan.steps]
    unknown = sorted(wanted - set(available))
    if unknown:
        raise SystemExit(
            f"unknown step(s): {', '.join(unknown)}; available: {', '.join(available)}"
        )
    return runner.Plan(tuple(step for step in plan.steps if step.name in wanted), plan.notes)


def main() -> int:
    """Run the requested plan and return its exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="changed-file subset for iteration")
    parser.add_argument("--skip-dist", action="store_true", help="full gate without build steps")
    parser.add_argument(
        "--only",
        help="comma-separated steps or verbs to run alone, e.g. lint or typecheck,format",
    )
    args = parser.parse_args()
    if args.only:
        names = tuple(part.strip() for part in args.only.split(",") if part.strip())
        plan = select_only(full_plan(sys.executable, skip_dist=False), names)
        return runner.run_plan(plan, ROOT)
    plan = fast_plan(sys.executable) if args.fast else full_plan(sys.executable, args.skip_dist)
    return runner.run_plan(plan, ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
