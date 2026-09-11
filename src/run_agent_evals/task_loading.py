"""Reading frozen evaluation tasks from a JSONL manifest.

Split out of ``models`` so that module holds the shapes and this one holds the parsing.
The loader also had to be broken up: as one function it ran to 42 lines and mixed four
separate validations - the JSON itself, the fixture path, the prompt, and the verifier
commands - so a failure in any of them produced one undifferentiated error.

``{python}`` is resolved here because a manifest has to be portable: the same task file is
used by a developer on Windows and by CI on Linux, and the interpreter running the suite
is the one that should run the verifier.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from run_agent_evals.models import PYTHON_PLACEHOLDER, FrozenTask


def load_tasks(path: str | Path) -> tuple[FrozenTask, ...]:
    """Read every task in a manifest, refusing the first line that cannot be used."""
    task_path = Path(path)
    seen: set[str] = set()
    return tuple(
        _task_from(line, number, task_path.parent, seen)
        for number, line in enumerate(task_path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip()
    )


def _task_from(line: str, number: int, root: Path, seen: set[str]) -> FrozenTask:
    """One manifest line as a task, with every field validated by name.

    ``root`` is the manifest's directory, because a fixture path is written relative to
    the manifest rather than to the working directory.
    """
    payload = _decode(line, number)
    task_id = str(payload["id"])
    if task_id in seen:
        raise ValueError(f"duplicate task id {task_id!r} on line {number}")
    seen.add(task_id)
    return FrozenTask(
        id=task_id,
        fixture=_fixture(payload, root, task_id),
        prompt=_prompt(payload, task_id),
        verify=_verify(payload, task_id),
        tags=tuple(str(tag) for tag in payload.get("tags", [])),
        timeout_seconds=_timeout(payload, task_id),
    )


def _decode(line: str, number: int) -> Mapping[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid evaluation task on line {number}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid evaluation task on line {number}: expected a JSON object")
    return payload


def _fixture(payload: Mapping[str, Any], root: Path, task_id: str) -> Path:
    fixture = (root / str(payload["fixture"])).resolve()
    if not fixture.is_dir():
        raise ValueError(f"task {task_id!r} fixture does not exist: {fixture}")
    return fixture


def _prompt(payload: Mapping[str, Any], task_id: str) -> str:
    prompt = str(payload["prompt"])
    if not prompt.strip():
        raise ValueError(f"task {task_id!r} requires a prompt")
    return prompt


def _verify(payload: Mapping[str, Any], task_id: str) -> tuple[tuple[str, ...], ...]:
    commands = tuple(
        tuple(sys.executable if part == PYTHON_PLACEHOLDER else str(part) for part in command)
        for command in payload["verify"]
    )
    if not commands or any(not command for command in commands):
        raise ValueError(f"task {task_id!r} requires verifier commands")
    return commands


def _timeout(payload: Mapping[str, Any], task_id: str) -> float:
    value = float(payload.get("timeout_seconds", 120))
    if value <= 0:
        raise ValueError(f"task {task_id!r} timeout must be positive")
    return value
