"""Append-only candidate storage and trusted probe boundaries."""

import json
import os

import pytest
from pydantic import ValidationError

from run_agent_extensions.experience.candidates import (
    CANDIDATE_SCHEMA,
    MAX_CHANGED_CHARS,
    CandidateError,
    CandidateOperation,
    ProjectProbe,
    SkillCandidateStore,
    materialize_operations,
)
from run_agent_extensions.experience.tools import SkillCall


def test_candidate_store_appends_schema_events_and_deduplicates_blobs(tmp_path):
    store = SkillCandidateStore(tmp_path / "candidates")
    operation = CandidateOperation("add", new_text="candidate body")
    first = store.create(
        scope="project",
        name="deploy",
        source_session="session-1",
        source_run="run-1",
        base_content=None,
        operations=(operation,),
    )
    second = store.create(
        scope="project",
        name="deploy-two",
        source_session="session-1",
        source_run="run-1",
        base_content=None,
        operations=(operation,),
    )
    verified = store.transition(first.candidate_id, "verified", report_id="report-1")

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert [row["type"] for row in rows] == ["candidate", "candidate", "status"]
    assert all(row["schema"] == CANDIDATE_SCHEMA for row in rows)
    assert rows[0]["source"] == {"session": "session-1", "run": "run-1"}
    assert rows[0]["content_blob"] == f"blobs/{first.candidate_digest}.md"
    assert len(list(store.blobs.glob("*.md"))) == 1
    assert store.content(first) == store.content(second) == "candidate body"
    assert verified.status == "verified" and verified.report_id == "report-1"
    assert [event.status for event in verified.status_events] == ["cold", "verified"]


def test_operations_are_bounded_and_require_unique_matches():
    with pytest.raises(CandidateError, match="at most 8"):
        materialize_operations("", tuple(CandidateOperation("add", new_text="x") for _ in range(9)))
    with pytest.raises(CandidateError, match="limit"):
        materialize_operations(
            "", (CandidateOperation("add", new_text="x" * (MAX_CHANGED_CHARS + 1)),)
        )
    with pytest.raises(CandidateError, match="matched 2"):
        materialize_operations(
            "same same", (CandidateOperation("replace", old_text="same", new_text="new"),)
        )
    assert (
        materialize_operations(
            "alpha", (CandidateOperation("replace", old_text="alpha", new_text="beta"),)
        )
        == "beta"
    )


def test_project_probe_rejects_untrusted_absolute_traversal_and_redirects(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "facts.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    trusted = ProjectProbe(project, trusted=True)
    evidence = trusted.digest("facts.txt")
    assert trusted.verify(evidence)
    assert trusted.grep("facts.txt", "beta") == ("2:beta",)

    for path in (str((project / "facts.txt").resolve()), "../facts.txt"):
        with pytest.raises(CandidateError, match="relative"):
            trusted.read(path)
    with pytest.raises(CandidateError, match="trusted"):
        ProjectProbe(project, trusted=False).read("facts.txt")

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = project / "redirect.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("host cannot create symlinks")
    with pytest.raises(CandidateError, match="symlink or junction"):
        trusted.read("redirect.txt")


def test_skill_manage_schema_only_allows_list_view_and_propose():
    for action in ("list", "view", "propose"):
        assert SkillCall.model_validate({"action": action}).action == action
    for action in ("create", "edit", "patch", "delete", "write_file"):
        with pytest.raises(ValidationError):
            SkillCall.model_validate({"action": action})
