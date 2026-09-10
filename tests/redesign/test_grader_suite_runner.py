"""RED: the dual propositions must hold against a real grader (T-004).

The classification core was tested against a fake suite, which proves it decides
correctly but not that it is connected to anything. These tests drive the real grader
of a real task in evals/coding/tasks, so the capability boundary is exercised end to
end: per-test outcomes come back from pytest, the reference solution proves FAIL_TO_PASS
and breaks nothing, and a partial fix is not accepted as a fix.
"""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from run_agent_evals.grader_runner import GraderSuiteRunner, parse_junit
from run_agent_evals.task_spec import TaskSpec, load_task_spec, materialize_environment
from run_agent_evals.verifier import DualPropositionVerifier

TASKS = Path(__file__).resolve().parents[2] / "evals" / "coding" / "tasks"
MIGRATION = "python-config-migration"


def build(tmp_path: Path, *, use_reference: bool) -> tuple[TaskSpec, Path]:
    """A workspace holding the pristine environment, optionally with the reference."""
    spec = load_task_spec(TASKS / MIGRATION)
    workspace = tmp_path / ("reference" if use_reference else "pristine")
    materialize_environment(spec, workspace)
    if use_reference:
        for source in sorted(spec.reference.iterdir()):
            shutil.copy2(source, workspace / source.name)
    return spec, workspace


def build_partial(tmp_path: Path) -> tuple[TaskSpec, Path]:
    """The reference fix with one file left unmigrated - a plausible half-done answer."""
    spec, workspace = build(tmp_path, use_reference=True)
    shutil.copy2(spec.environment / "client.py", workspace / "client.py")
    return spec, workspace


async def test_the_runner_reports_one_outcome_per_test(tmp_path):
    spec, pristine = build(tmp_path, use_reference=False)

    outcomes = await GraderSuiteRunner(spec).run(pristine)

    assert outcomes, "the grader reported no tests at all"
    assert all(isinstance(passed, bool) for passed in outcomes.values())
    assert any("::" in name for name in outcomes), outcomes


async def test_the_reference_solution_proves_the_fix_and_breaks_nothing(tmp_path):
    spec, pristine = build(tmp_path, use_reference=False)
    _, reference = build(tmp_path, use_reference=True)

    verdict = await DualPropositionVerifier(GraderSuiteRunner(spec), repeats=1).verify(
        pristine, reference, targets=spec.fail_to_pass
    )

    assert verdict.fail_to_pass, f"no test went from failing to passing: {verdict}"
    assert verdict.unmet_targets == frozenset(), f"a declared target was not met: {verdict}"
    assert verdict.newly_failing == frozenset(), f"the reference broke tests: {verdict}"
    assert verdict.succeeded is True


async def test_a_partial_fix_is_not_accepted_as_a_fix(tmp_path):
    spec, pristine = build(tmp_path, use_reference=False)
    _, partial = build_partial(tmp_path)

    verdict = await DualPropositionVerifier(GraderSuiteRunner(spec), repeats=1).verify(
        pristine, partial, targets=spec.fail_to_pass
    )

    assert verdict.still_failing, f"the unmigrated file should leave a test failing: {verdict}"
    assert verdict.unmet_targets, f"a declared target was left unsatisfied: {verdict}"
    assert verdict.is_fix_proven is False
    assert verdict.succeeded is False


async def test_a_grader_that_exceeds_its_budget_yields_no_evidence(tmp_path):
    """A timed-out grader produces no per-test evidence, so nothing is proven.

    This branch was written defensively while implementing the runner, so its test
    came after the code rather than before; it is recorded as a deviation from the
    RED-first order rather than presented as a RED.
    """
    spec, pristine = build(tmp_path, use_reference=False)
    impatient = replace(spec, budget_seconds=0.001)

    outcomes = await GraderSuiteRunner(impatient).run(pristine)
    assert outcomes == {}

    verdict = await DualPropositionVerifier(GraderSuiteRunner(impatient), repeats=1).verify(
        pristine, pristine, targets=spec.fail_to_pass
    )
    assert verdict.is_fix_proven is False
    assert verdict.succeeded is False


async def test_a_task_declares_which_tests_prove_the_fix(tmp_path):
    spec, _ = build(tmp_path, use_reference=False)

    assert spec.fail_to_pass, "the migration task must declare its FAIL_TO_PASS set"
    assert all(name.startswith("test_") for name in spec.fail_to_pass), spec.fail_to_pass


def test_a_skipped_test_is_not_reported_as_passing(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuites><testsuite>'
        '<testcase classname="g" name="real"><failure message="x"/></testcase>'
        '<testcase classname="g" name="ok"/>'
        '<testcase classname="g" name="skipped"><skipped message="host"/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    outcomes = parse_junit(report)

    assert outcomes == {"g::real": False, "g::ok": True}
    assert "g::skipped" not in outcomes


def test_a_missing_report_yields_no_evidence(tmp_path):
    assert parse_junit(tmp_path / "absent.xml") == {}


async def test_a_workspace_missing_a_declared_artifact_yields_no_evidence(tmp_path):
    spec, pristine = build(tmp_path, use_reference=False)
    (pristine / "defaults.py").unlink()

    with pytest.raises(FileNotFoundError):
        # The runner refuses rather than grading a tree it could not assemble.
        GraderSuiteRunner(spec).run_blocking(pristine)
