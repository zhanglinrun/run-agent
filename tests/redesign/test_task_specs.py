"""Task specification layout and reference admission.

Covers the logical task layout of plan 8.3 and the admission properties of 8.5:
the authoritative grader lives in the task, never in the workspace the agent can
edit, and only the artifacts the manifest declares are carried into grading.
"""

import json
from pathlib import Path

import pytest

from run_agent_evals.task_spec import (
    TaskSpec,
    admit_task,
    load_task_spec,
    materialize_environment,
)

TASK_TOML = """
[task]
id = "python-slug-normalization"
version = "1"
kind = "bug-fix"
budget_seconds = 120
artifacts = ["slug.py"]

[grader]
command = ["{python}", "-m", "pytest", "-q", "grader"]
"""

BUGGY = "def slugify(text):\n    return text\n"

FIXED = (
    "import re\n\n\n"
    "def slugify(text):\n"
    '    return re.sub(r"[\\s_]+", "-", text.strip()).strip("-").lower()\n'
)

VISIBLE_TEST = (
    "from slug import slugify\n\n\ndef test_spaces():\n    assert slugify('a b') == 'a-b'\n"
)

GRADER_TEST = (
    "from slug import slugify\n\n\n"
    "def test_spaces():\n    assert slugify('a  b') == 'a-b'\n\n\n"
    "def test_underscores():\n    assert slugify('a__b') == 'a-b'\n\n\n"
    "def test_edges():\n    assert slugify('  a-b  ') == 'a-b'\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    write(root / "task.toml", TASK_TOML)
    write(root / "instruction.md", "Make slugify collapse whitespace runs into single hyphens.\n")
    write(
        root / "rubric.json",
        json.dumps({"required": ["test_spaces", "test_underscores", "test_edges"]}) + "\n",
    )
    write(root / "environment" / "slug.py", BUGGY)
    write(root / "environment" / "tests" / "test_slug.py", VISIBLE_TEST)
    write(root / "grader" / "test_slug_grader.py", GRADER_TEST)
    write(root / "reference" / "slug.py", FIXED)
    return root


def prepared(task_dir: Path, tmp_path: Path) -> tuple[TaskSpec, Path]:
    spec = load_task_spec(task_dir)
    workspace = tmp_path / "workspace"
    materialize_environment(spec, workspace)
    return spec, workspace


def test_load_task_spec_reads_the_documented_layout(task_dir: Path) -> None:
    spec = load_task_spec(task_dir)
    assert isinstance(spec, TaskSpec)
    assert spec.id == "python-slug-normalization"
    assert spec.version == "1"
    assert "single hyphens" in spec.instruction
    assert spec.budget_seconds == 120
    assert spec.artifacts == ("slug.py",)
    assert spec.environment == task_dir / "environment"
    assert spec.grader == task_dir / "grader"
    assert spec.reference == task_dir / "reference"


def test_environment_copy_carries_only_the_environment(task_dir: Path, tmp_path: Path) -> None:
    spec, workspace = prepared(task_dir, tmp_path)
    assert (workspace / "slug.py").read_text(encoding="utf-8") == BUGGY
    assert (workspace / "tests" / "test_slug.py").exists()
    for leaked in ("grader", "reference", "task.toml", "rubric.json"):
        assert not (workspace / leaked).exists(), leaked


def test_no_op_solution_is_not_admitted(task_dir: Path, tmp_path: Path) -> None:
    spec, workspace = prepared(task_dir, tmp_path)
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False
    assert outcome.reason


def test_reference_solution_is_admitted(task_dir: Path, tmp_path: Path) -> None:
    spec, workspace = prepared(task_dir, tmp_path)
    (workspace / "slug.py").write_text(FIXED, encoding="utf-8")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is True, outcome.output


def test_answer_text_only_solution_is_not_admitted(task_dir: Path, tmp_path: Path) -> None:
    spec, workspace = prepared(task_dir, tmp_path)
    write(workspace / "ANSWER.md", "Done: slugify now collapses whitespace runs.\n")
    write(workspace / "tests" / "test_slug.py", "def test_ok():\n    assert True\n")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False


def test_workspace_tampering_cannot_weaken_the_authoritative_grader(
    task_dir: Path, tmp_path: Path
) -> None:
    spec, workspace = prepared(task_dir, tmp_path)
    write(workspace / "tests" / "test_slug.py", "def test_ok():\n    assert True\n")
    write(workspace / "conftest.py", "collect_ignore_glob = ['*']\n")
    write(workspace / "pytest.ini", "[pytest]\ntestpaths = nonexistent\n")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is False, outcome.output


def test_grader_runs_outside_the_workspace(task_dir: Path, tmp_path: Path) -> None:
    """Grading must not depend on the workspace being writable or present afterwards."""
    spec, workspace = prepared(task_dir, tmp_path)
    (workspace / "slug.py").write_text(FIXED, encoding="utf-8")
    outcome = admit_task(spec, workspace)
    assert outcome.admitted is True
    assert outcome.graded_directory is not None
    assert Path(outcome.graded_directory) != workspace
    assert not Path(outcome.graded_directory).is_relative_to(workspace)
