"""The JSONL task loader must resolve the {python} placeholder.

The directory task format resolves it - task_spec replaces ``{python}`` with the
running interpreter - but the JSONL loader passed verifier commands through
untouched. The first real-model run exposed it: the model fixed the bug and reported
"2 passed", yet the trial was recorded as an error with an empty verifier list and
``[WinError 2]``, because ``{python}`` was executed as if it were a program name.

A verifier that never runs is worse than one that fails, because the trial still looks
like it produced evidence.
"""

import json
import sys
from pathlib import Path

from run_agent_evals.task_loading import load_tasks


def write_task(tmp_path: Path, verify: list[list[str]]) -> Path:
    fixture = tmp_path / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "subject.py").write_text("value = 1\n", encoding="utf-8")
    manifest = tmp_path / "tasks.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "id": "sample",
                "fixture": "fixture",
                "prompt": "fix it",
                "verify": verify,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def test_the_python_placeholder_becomes_this_interpreter(tmp_path):
    tasks = load_tasks(write_task(tmp_path, [["{python}", "-m", "pytest", "-q"]]))

    assert tasks[0].verify == ((sys.executable, "-m", "pytest", "-q"),)


def test_an_explicit_program_is_left_alone(tmp_path):
    tasks = load_tasks(write_task(tmp_path, [["ruff", "check"]]))

    assert tasks[0].verify == (("ruff", "check"),)


def test_a_placeholder_in_a_later_argument_is_also_resolved(tmp_path):
    tasks = load_tasks(write_task(tmp_path, [["echo", "{python}"]]))

    assert tasks[0].verify == (("echo", sys.executable),)
