"""Trial-level workspace escapes: contaminated evidence never counts as a pass.

Everything here is offline. ``TranscriptExecutor`` copies the frozen reference when a
Skill is installed and writes a session transcript; the hidden graders still run for
real, which is why these reports use ``repeats=1``. No provider or model is contacted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from run_agent_coding.host.evaluation import EvaluationRequest
from run_agent_evals.evolution import (
    EvolutionEvaluationService,
    _scan_workspace_escape,
    _trial_from_json,
    rebuild_evolution_report,
    reduce_evolution_trials,
)
from run_agent_evals.models import ExecutionFailure, ExecutionResult
from run_agent_evals.task_spec import TaskSpec
from run_agent_extensions.experience.candidates import CandidateOperation, SkillCandidateStore
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots

SUITE = Path("evals/evolution/config.toml")
SKILL = "config-skill"
SESSION_ID = "eval-escape-session"
FORMAL = (
    "---\nname: config-skill\ndescription: solve config tasks\ncreated_by: evolution\n---\n"
    "Use the project contract.\n"
)

ToolCalls = list[tuple[str, dict[str, Any]]]


def _write_session(workspace: Path, calls: ToolCalls, *, name: str = SESSION_ID) -> Path:
    """Write one assistant transcript with real ``toolCall`` content items."""
    directory = workspace / ".run" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "seq": index,
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": f"call-{index}",
                            "name": tool,
                            "arguments": arguments,
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )
        for index, (tool, arguments) in enumerate(calls, start=1)
    ]
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TranscriptExecutor:
    """Copies the reference when a Skill is installed, then writes one transcript."""

    def __init__(
        self,
        build: Callable[[Path], ToolCalls],
        *,
        session_id: str | None = SESSION_ID,
    ) -> None:
        self.build = build
        self.session_id = session_id

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> Mapping[str, Any]:
        if (state_root / "skills" / SKILL / "SKILL.md").is_file():
            for source in sorted(spec.reference.iterdir()):
                target = workspace / source.name
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
        metadata: dict[str, Any] = {
            "calls": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "known_cost": 0.01,
            "cost": 0.01,
            "workspace": str(workspace),
        }
        if self.session_id is not None:
            _write_session(workspace, self.build(workspace), name=self.session_id)
            metadata["session_id"] = self.session_id
        return metadata


class FailingTranscriptExecutor(TranscriptExecutor):
    """Writes the transcript, then fails the trial like a real executor failure."""

    async def execute(self, spec: TaskSpec, workspace: Path, state_root: Path) -> Mapping[str, Any]:
        metadata = await super().execute(spec, workspace, state_root)
        raise ExecutionFailure(
            "boom",
            ExecutionResult(output="", metadata=dict(metadata)),
        )


def _service(
    tmp_path: Path, executor: TranscriptExecutor, *, repeats: int = 1
) -> tuple[EvolutionEvaluationService, str]:
    """A paired evaluator whose baseline arm installs no Skill, so it always fails."""
    candidates = SkillCandidateStore(tmp_path / "experience" / "candidates")
    skills = SkillManager(SkillRoots(user=tmp_path / "skills", project=tmp_path / "project-skills"))
    candidate = candidates.create(
        scope="user",
        name=SKILL,
        source_session="session",
        source_run="run",
        base_content=None,
        operations=(CandidateOperation("add", new_text=FORMAL),),
        candidate_content=FORMAL,
    )
    service = EvolutionEvaluationService(
        suite=SUITE,
        output_root=tmp_path / "reports",
        candidates=candidates,
        skills=skills,
        executor=executor,
        repeats=repeats,
    )
    return service, candidate.candidate_id


def _request(candidate_id: str, candidate_digest: str) -> EvaluationRequest:
    return EvaluationRequest(
        candidate_id=candidate_id,
        content_hash=candidate_digest,
        baseline="none",
        suite="config",
        suite_version="2",
        budget_seconds=30,
    )


async def _frozen_report(
    tmp_path: Path, executor: TranscriptExecutor
) -> tuple[Path, list[dict[str, Any]]]:
    """Run one paired campaign and return its frozen root plus its trial rows."""
    service, candidate_id = _service(tmp_path, executor)
    candidate = service.candidates.require(candidate_id)
    report_id = await service.submit(_request(candidate_id, candidate.candidate_digest))
    root = service.output_root / report_id
    rows = json.loads((root / "trials.json").read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    return root, rows


def _rewrite_frozen(path: Path, payload: object) -> None:
    """Rewrite one frozen file and repair its inventory row, defeating hashing only."""
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.write_bytes(data)
    inventory_path = path.parent / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    for item in inventory["files"]:
        if item["path"] == path.name:
            item["size"] = len(data)
            item["sha256"] = hashlib.sha256(data).hexdigest()
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _bare_workspace(tmp_path: Path) -> Path:
    """A workspace directory shaped like a real trial's, with no shared state."""
    workspace = tmp_path / "reports" / "candidate" / "task" / "0" / "candidate"
    workspace.mkdir(parents=True)
    return workspace


@pytest.mark.anyio
async def test_an_out_of_workspace_read_is_flagged_and_blocks_the_gate(tmp_path: Path) -> None:
    """A trial that reads the task directory is contaminated, not a pass."""
    outside = tmp_path / "evals" / "reference" / "pathnorm.py"
    executor = TranscriptExecutor(lambda workspace: [("read", {"path": str(outside)})])

    root, rows = await _frozen_report(tmp_path, executor)

    assert len(rows) == 12
    assert all(row["workspace_escape"] is True for row in rows)
    assert all(row["succeeded"] is False for row in rows)
    assert all(row["infrastructure_error"] is None for row in rows)
    assert all(row["metadata"]["escape_scan"] == "ok" for row in rows)
    assert all(str(outside) in row["metadata"]["workspace_escape_targets"] for row in rows)

    document = rebuild_evolution_report(root)
    summary = document["summary"]
    assert document["passed"] is False
    assert summary["selection_gate"]["passed"] is False
    assert summary["workspace_escapes"] == 12
    for task in summary["tasks"].values():
        assert task["baseline"]["escapes"] == 1
        assert task["candidate"]["escapes"] == 1
        assert task["baseline"]["passes"] == 0
        assert task["candidate"]["passes"] == 0


@pytest.mark.anyio
async def test_workspace_internal_targets_are_never_escapes(tmp_path: Path) -> None:
    """The workspace itself, its ``.run`` directory and inner traversal are allowed."""

    def build(workspace: Path) -> ToolCalls:
        return [
            ("read", {"path": "pathnorm.py"}),
            ("read", {"path": str(workspace / "pathnorm.py")}),
            ("read", {"path": ".run/sessions/notes.jsonl"}),
            ("read", {"path": str(workspace / ".run" / "context" / "blobs" / "d.txt")}),
            ("read", {"path": "tests/../pathnorm.py"}),
            ("bash", {"command": f'cd "{workspace}" && python -m pytest -q'}),
            ("bash", {"command": 'find . -type f -not -path "*/.git/*"'}),
            ("grep", {"pattern": "normalize", "path": "tests"}),
        ]

    root, rows = await _frozen_report(tmp_path, TranscriptExecutor(build))

    assert all(row["workspace_escape"] is False for row in rows)
    assert all(row["metadata"]["escape_scan"] == "ok" for row in rows)
    assert all("workspace_escape_targets" not in row["metadata"] for row in rows)
    document = rebuild_evolution_report(root)
    assert document["passed"] is True
    assert "workspace_escapes" not in document["summary"]
    for task in document["summary"]["tasks"].values():
        assert "escapes" not in task["baseline"]
        assert "escapes" not in task["candidate"]


def test_traversal_and_bash_drive_paths_are_escapes(tmp_path: Path) -> None:
    """``..`` traversal and a drive path inside a command both leave the workspace."""
    workspace = _bare_workspace(tmp_path)
    calls: ToolCalls = [
        ("read", {"path": "../../../../outside.txt"}),
        ("bash", {"command": f'type "{tmp_path / "outside.txt"}"'}),
        ("read", {"path": "notes/../inside.txt"}),
        ("bash", {"command": "dir /s /b ..\\.."}),
        ("bash", {"command": "python -c \"open('\\\\tmp\\\\x')\""}),
    ]
    _write_session(workspace, calls)

    escaped, evidence, scan = _scan_workspace_escape(workspace, {"session_id": SESSION_ID})

    assert escaped is True
    assert scan == "ok"
    assert "../../../../outside.txt" in evidence
    assert str(tmp_path / "outside.txt") in evidence
    assert "..\\.." in evidence
    assert "inside.txt" not in evidence
    # Regex and code fragments that only start with a separator stay out.
    assert "\\\\tmp" not in evidence


def test_a_missing_session_id_falls_back_to_the_newest_session(tmp_path: Path) -> None:
    """Without an executor session id the newest non-dotfile transcript is scanned."""
    workspace = _bare_workspace(tmp_path)
    older = _write_session(workspace, [("read", {"path": "inside.py"})], name="eval-old")
    newer = _write_session(
        workspace, [("read", {"path": str(tmp_path / "outside.txt")})], name="eval-new"
    )
    (workspace / ".run" / "sessions" / ".eval-new.jsonl.lock").write_text("", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    escaped, evidence, scan = _scan_workspace_escape(workspace, {})

    assert escaped is True
    assert scan == "ok"
    assert str(tmp_path / "outside.txt") in evidence


def test_escape_evidence_is_capped(tmp_path: Path) -> None:
    """A hostile transcript cannot grow one trial row without a bound."""
    workspace = _bare_workspace(tmp_path)
    calls: ToolCalls = [
        ("read", {"path": str(tmp_path / "outside" / f"file-{index:02d}.py")})
        for index in range(20)
    ]
    _write_session(workspace, calls)

    escaped, evidence, scan = _scan_workspace_escape(workspace, {"session_id": SESSION_ID})

    assert escaped is True
    assert scan == "ok"
    assert "+14 more" in evidence
    assert len(evidence) <= 1_000


def test_a_workspace_without_a_readable_session_is_unavailable(tmp_path: Path) -> None:
    """No session file means ``unavailable``, which never counts as an escape."""
    empty = _bare_workspace(tmp_path)
    sessions = empty / ".run" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / ".hidden.jsonl").write_text("{}\n", encoding="utf-8")

    assert _scan_workspace_escape(empty, {}) == (False, None, "unavailable")

    missing = tmp_path / "missing-workspace"
    missing.mkdir()
    assert _scan_workspace_escape(missing, {}) == (False, None, "unavailable")


@pytest.mark.anyio
async def test_a_failed_trial_keeps_its_error_and_still_records_the_escape(tmp_path: Path) -> None:
    """An infrastructure error stays an error; the escape is counted next to it."""
    outside = tmp_path / "outside.txt"
    executor = FailingTranscriptExecutor(lambda workspace: [("read", {"path": str(outside)})])

    root, rows = await _frozen_report(tmp_path, executor)

    assert all(row["infrastructure_error"].startswith("ExecutionFailure: boom") for row in rows)
    assert all(row["workspace_escape"] is True for row in rows)
    assert all(row["succeeded"] is False for row in rows)
    assert all(row["metadata"]["escape_scan"] == "ok" for row in rows)

    summary = rebuild_evolution_report(root)["summary"]
    assert summary["workspace_escapes"] == 12
    assert summary["selection_gate"]["infrastructure_errors"] == 12
    assert summary["selection_gate"]["passed"] is False


@pytest.mark.anyio
async def test_a_trial_without_a_session_records_unavailable(tmp_path: Path) -> None:
    """A stub executor that writes no transcript keeps its campaign rebuildable."""
    executor = TranscriptExecutor(lambda workspace: [], session_id=None)

    root, rows = await _frozen_report(tmp_path, executor)

    assert len(rows) == 12
    assert all(row["workspace_escape"] is False for row in rows)
    assert all(row["metadata"]["escape_scan"] == "unavailable" for row in rows)
    assert rebuild_evolution_report(root)["passed"] is True


@pytest.mark.anyio
async def test_legacy_trials_without_the_escape_field_still_reduce_and_rebuild(
    tmp_path: Path,
) -> None:
    """Rows frozen before ``workspace_escape`` existed default to False end to end."""
    executor = TranscriptExecutor(lambda workspace: [("read", {"path": "pathnorm.py"})])
    root, rows = await _frozen_report(tmp_path, executor)

    legacy = [
        {key: value for key, value in row.items() if key != "workspace_escape"} for row in rows
    ]
    assert all("workspace_escape" not in row for row in legacy)
    _rewrite_frozen(root / "trials.json", legacy)

    document = rebuild_evolution_report(root)
    assert document["passed"] is True
    assert "workspace_escapes" not in document["summary"]

    trials = [_trial_from_json(row) for row in legacy]
    assert all(trial.workspace_escape is False for trial in trials)
    assert reduce_evolution_trials(trials, repeats=1) == document["summary"]
