"""Run bench reaches the task pipeline: a task directory is
enumerated, each ready task is graded through the dual propositions, and the result
carries the rates the report needs.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from run_agent_evals.suite import TaskSuite

TASKS = Path(__file__).resolve().parents[2] / "evals" / "coding" / "tasks"
MIGRATION = "python-config-migration"


def one_task_directory(tmp_path: Path, *, status: str = "ready") -> Path:
    """A suite directory holding exactly one task, so grading stays cheap."""
    root = tmp_path / "tasks"
    root.mkdir()
    shutil.copytree(TASKS / MIGRATION, root / MIGRATION)
    manifest = {"tasks": [{"id": MIGRATION, "family": "migration", "status": status}]}
    (root / "tasks.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def test_the_suite_enumerates_the_ready_tasks(tmp_path):
    suite = TaskSuite(TASKS)

    ready = suite.ready()

    assert {spec.id for spec in ready} >= {
        "python-off-by-one",
        "python-slug-normalization",
        "python-config-precedence",
        MIGRATION,
    }
    assert all(spec.fail_to_pass for spec in ready), "every ready task declares its targets"


def test_a_planned_task_is_not_part_of_the_suite(tmp_path):
    assert TaskSuite(one_task_directory(tmp_path, status="planned")).ready() == ()


def test_the_reference_solution_passes_and_the_report_carries_rates(tmp_path):
    suite = TaskSuite(one_task_directory(tmp_path))

    report = suite.evaluate_default(use_reference=True)

    assert len(report.verdicts) == 1
    verdict = report.verdicts[0]
    assert verdict.task_id == MIGRATION
    assert verdict.succeeded is True
    assert verdict.unmet_targets == ()
    assert report.rate.successes == 1
    assert report.rate.trials == 1
    assert report.to_json()["tasks"][0]["task_id"] == MIGRATION


def test_the_pristine_environment_never_passes(tmp_path):
    suite = TaskSuite(one_task_directory(tmp_path))

    report = suite.evaluate_default(use_reference=False)

    assert report.verdicts[0].succeeded is False
    assert report.rate.successes == 0


def test_grading_does_not_write_into_the_task_directory(tmp_path):
    """Working trees belong in temp storage, not next to the task.

    Building them under the task root once put a .candidate and a .pristine copy of
    every task into the repository, and they were committed, because evals/ is not
    gitignored. A lower evaluation rate would not have revealed it.
    """
    root = one_task_directory(tmp_path)
    before = sorted(path.name for path in (root / MIGRATION).iterdir())

    TaskSuite(root).evaluate_default(use_reference=True)

    assert sorted(path.name for path in (root / MIGRATION).iterdir()) == before


def test_every_ready_task_declares_exactly_the_tests_that_fail_before_the_fix(tmp_path):
    """Targets must be measured, not guessed.

    The first hand-written declaration listed every grader test as a target, which
    included tests that already passed on the pristine tree. Those can never appear in
    FAIL_TO_PASS, so the task became impossible to satisfy and the suite reported one
    success out of four. This holds the declarations to the definition.
    """
    from run_agent_evals.grader_runner import GraderSuiteRunner
    from run_agent_evals.task_spec import materialize_environment

    for spec in TaskSuite(TASKS).ready():
        pristine = tmp_path / f"pristine-{spec.id}"
        materialize_environment(spec, pristine)
        outcomes = GraderSuiteRunner(spec).run_blocking(pristine)
        failing = sorted(name.split("::")[-1] for name, passed in outcomes.items() if not passed)
        assert sorted(spec.fail_to_pass) == failing, (
            f"{spec.id} declares {sorted(spec.fail_to_pass)} but these fail before the fix: {failing}"
        )


def test_run_bench_suite_is_reachable_from_the_command_line(tmp_path):
    root = one_task_directory(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-m", "run_agent_entry", "bench", "suite", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert MIGRATION in completed.stdout, completed.stdout
    assert "rate" in completed.stdout.lower()
