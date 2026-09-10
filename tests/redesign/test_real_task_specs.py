"""Reference admission checks over the real P0-4 task selection.

Every task marked ``ready`` in evals/coding/tasks/tasks.json must support the
three admission verdicts P0-4 asks for: a no-op solution fails, the reference
solution passes, and a solution that only changes prose the agent can see fails.
"""

import json
import shutil
from pathlib import Path

import pytest

from run_agent_evals.task_spec import admit_task, load_task_spec, materialize_environment

TASKS_ROOT = Path(__file__).resolve().parents[2] / "evals" / "coding" / "tasks"


def selection() -> dict:
    return json.loads((TASKS_ROOT / "tasks.json").read_text(encoding="utf-8"))


READY = [entry["id"] for entry in selection()["tasks"] if entry["status"] == "ready"]


def test_the_selection_names_between_six_and_ten_representative_tasks() -> None:
    entries = selection()["tasks"]
    assert 6 <= len(entries) <= 10, len(entries)
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert len(READY) >= 2, "at least two tasks need independent acceptance"


@pytest.mark.parametrize("task_id", READY)
def test_ready_tasks_have_the_documented_layout(task_id: str) -> None:
    root = TASKS_ROOT / task_id
    for required in ("task.toml", "instruction.md", "environment", "grader", "reference"):
        assert (root / required).exists(), required
    spec = load_task_spec(root)
    assert spec.id == task_id
    assert spec.instruction
    assert spec.artifacts


@pytest.mark.parametrize("task_id", READY)
def test_no_op_solution_is_rejected(task_id: str, tmp_path: Path) -> None:
    spec = load_task_spec(TASKS_ROOT / task_id)
    workspace = tmp_path / "workspace"
    materialize_environment(spec, workspace)
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False, f"{task_id} admitted a no-op solution"
    assert outcome.reason


@pytest.mark.parametrize("task_id", READY)
def test_reference_solution_is_admitted(task_id: str, tmp_path: Path) -> None:
    spec = load_task_spec(TASKS_ROOT / task_id)
    workspace = tmp_path / "workspace"
    materialize_environment(spec, workspace)
    for solution in sorted(spec.reference.rglob("*")):
        if solution.is_file():
            shutil.copy2(solution, workspace / solution.relative_to(spec.reference))
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is True, f"{task_id}: {outcome.reason}\n{outcome.output}"


@pytest.mark.parametrize("task_id", READY)
def test_prose_only_solution_is_rejected(task_id: str, tmp_path: Path) -> None:
    spec = load_task_spec(TASKS_ROOT / task_id)
    workspace = tmp_path / "workspace"
    materialize_environment(spec, workspace)
    (workspace / "ANSWER.md").write_text("Fixed it; all tests pass.\n", encoding="utf-8")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False, f"{task_id} admitted a prose-only answer"


@pytest.mark.parametrize("task_id", READY)
def test_editing_the_visible_tests_cannot_win_admission(task_id: str, tmp_path: Path) -> None:
    spec = load_task_spec(TASKS_ROOT / task_id)
    workspace = tmp_path / "workspace"
    materialize_environment(spec, workspace)
    for visible in sorted((workspace / "tests").glob("*.py")):
        visible.write_text("def test_ok() -> None:\n    assert True\n", encoding="utf-8")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False, f"{task_id} admitted a solution that only rewrote tests"
