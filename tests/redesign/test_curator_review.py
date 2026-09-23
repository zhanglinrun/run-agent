"""The bounded review: parsing, reconciliation, candidates, pruning and failures."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from tests.redesign.test_curator_state import NOW, make_env, write_skill

from run_agent_coding.host.evaluation import UnavailableEvaluation
from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceResult,
    InferenceUnavailable,
)
from run_agent_extensions.curator import (
    CuratorConfig,
    apply_review,
    build_run_payload,
    render_report_markdown,
    run_review,
)
from run_agent_extensions.curator.review import (
    HYGIENE_DISCLAIMER,
    mentions_name,
    parse_structured_summary,
    plan_review,
)
from run_agent_extensions.curator.transitions import TransitionResult
from run_agent_extensions.experience.candidates import ProjectProbe, SkillCandidateStore
from run_agent_extensions.experience.evolution import SkillEvolution

GOOD_ANSWER = """
I reviewed the library.

## Structured summary (required)
```yaml
consolidations:
  - from: old-extract
    into: document-tools
    reason: one section of the umbrella covers it
prunings:
  - name: dead-skill
    reason: obsolete
```
"""


class StubInference:
    """One canned completion, or a refusal, without any provider."""

    def __init__(self, text: str = GOOD_ANSWER, *, available: bool = True, error=None, delay=0.0):
        self.text = text
        self.available = available
        self.error = error
        self.delay = delay
        self.requests: list[object] = []

    async def complete(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return InferenceResult(text=self.text, model="stub-model", snapshot_id="snapshot-1")


def make_evolution(env, tmp_path, *, inference=None, project_enabled=True):
    return SkillEvolution(
        candidates=SkillCandidateStore(tmp_path / "candidates"),
        skills=env.manager,
        probe=ProjectProbe(env.project, trusted=True),
        evaluation=UnavailableEvaluation(),
        project_enabled=project_enabled,
        inference=inference,
    )


def seed_library(env):
    """One absorbed Skill, one umbrella that names it, and one dead Skill."""
    write_skill(env.user_root, "old-extract", body="Extract text from PDFs.")
    write_skill(
        env.user_root,
        "document-tools",
        body="Umbrella for document work. See old-extract for PDF extraction.",
    )
    write_skill(env.user_root, "dead-skill", body="Nothing depends on this.")
    return env.library.view()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GOOD_ANSWER, None),
        ("no block here", "no ```yaml block"),
        ("", "no output"),
    ],
)
def test_parsing_reports_the_contract(text, expected):
    summary = parse_structured_summary(text)
    if expected is None:
        assert summary.error is None
        assert summary.consolidations == (
            {
                "from": "old-extract",
                "into": "document-tools",
                "reason": "one section of the umbrella covers it",
            },
        )
        assert summary.prunings == ({"name": "dead-skill", "reason": "obsolete"},)
    else:
        assert expected in (summary.error or "")
        assert summary.consolidations == () and summary.prunings == ()


def test_parsing_accepts_empty_lists_and_quotes():
    summary = parse_structured_summary(
        '```yaml\nconsolidations: []\nprunings:\n  - name: "dead-skill"\n```'
    )
    assert summary.error is None
    assert summary.consolidations == ()
    assert summary.prunings == ({"name": "dead-skill", "reason": ""},)


def test_parsing_of_a_bad_block_is_an_error_with_empty_lists():
    summary = parse_structured_summary("```yaml\nconsolidations: 3\n```")
    assert summary.error is not None
    assert summary.consolidations == () and summary.prunings == ()


def test_incomplete_entries_are_dropped_and_counted():
    summary = parse_structured_summary(
        "```yaml\nconsolidations:\n  - from: only-source\nprunings:\n  - reason: no name\n```"
    )
    assert summary.error is None
    assert summary.incomplete == 2
    assert summary.consolidations == () and summary.prunings == ()


def test_mentions_name_uses_word_boundaries():
    assert mentions_name("see api-design here", "api") is False
    assert mentions_name("use api here", "api") is True
    assert mentions_name("use open_webui_setup here", "open-webui-setup") is True
    assert mentions_name("latest", "test") is False


def test_reconciliation_uses_body_evidence(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = plan_review(parse_structured_summary(GOOD_ANSWER), view=view)
    assert outcome.error is None
    plan = outcome.consolidations[0]
    assert (plan.name, plan.into, plan.evidence) == ("old-extract", "document-tools", "absorbed")
    assert plan.operations[0].action == "add"
    assert plan.operations[0].old_text == ""
    assert "## Absorbed: old-extract" in plan.operations[0].new_text


def test_reconciliation_without_evidence_keeps_the_declared_target(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-extract", body="Extract text.")
    write_skill(env.user_root, "document-tools", body="Umbrella for documents.")
    view = env.library.view()
    outcome = plan_review(parse_structured_summary(GOOD_ANSWER), view=view)
    assert outcome.consolidations[0].evidence == "unverified"


def test_reconciliation_prefers_the_skill_that_actually_mentions_it(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-extract", body="Extract text.")
    write_skill(env.user_root, "document-tools", body="Umbrella for documents.")
    write_skill(env.user_root, "pdf-suite", body="Absorbs old-extract now.")
    view = env.library.view()
    outcome = plan_review(parse_structured_summary(GOOD_ANSWER), view=view)
    assert outcome.consolidations[0].into == "pdf-suite"
    assert outcome.consolidations[0].evidence == "absorbed-elsewhere"


def test_reconciliation_drops_unknown_names(tmp_path):
    env = make_env(tmp_path)
    seed_library(env)
    answer = (
        "```yaml\nconsolidations:\n  - from: ghost\n    into: document-tools\n"
        "    reason: r\nprunings:\n  - name: also-ghost\n    reason: r\n```"
    )
    outcome = plan_review(parse_structured_summary(answer), view=env.library.view())
    assert outcome.consolidations == () and outcome.prunings == ()
    reasons = {item["name"]: item["reason"] for item in outcome.skipped}
    assert reasons["ghost"] == "consolidation source is not present"
    assert reasons["also-ghost"] == "pruning target is not present"


def test_reconciliation_never_prunes_a_consolidation_source(tmp_path):
    env = make_env(tmp_path)
    seed_library(env)
    answer = (
        "```yaml\nconsolidations:\n  - from: old-extract\n    into: document-tools\n"
        "    reason: r\nprunings:\n  - name: old-extract\n    reason: r\n```"
    )
    outcome = plan_review(parse_structured_summary(answer), view=env.library.view())
    assert outcome.prunings == ()
    assert any("named in a consolidation" in item["reason"] for item in outcome.skipped)


def test_reconciliation_skips_a_self_consolidation(tmp_path):
    env = make_env(tmp_path)
    seed_library(env)
    answer = (
        "```yaml\nconsolidations:\n  - from: old-extract\n    into: old-extract\n    reason: r\n```"
    )
    outcome = plan_review(parse_structured_summary(answer), view=env.library.view())
    assert outcome.consolidations == ()
    assert outcome.skipped[0]["reason"] == "consolidation target is the source"


async def test_unavailable_inference_degrades_without_touching_anything(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    before = {
        record.key: env.manager.main_content(record.scope, record.name) for record in view.records
    }

    outcome = await run_review(
        config=env.config, view=view, inference=StubInference(available=False)
    )
    assert outcome.error is not None and "no InferenceService" in outcome.error
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=make_evolution(env, tmp_path),
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
    )
    assert application.consolidated == () and application.pruned == ()
    for record in env.library.records():
        assert env.manager.main_content(record.scope, record.name) == before[record.key]
    assert env.library.archived_names("user") == ()


async def test_busy_inference_is_reported_and_changes_nothing(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(
        config=env.config, view=view, inference=StubInference(error=InferenceBusy("busy"))
    )
    assert outcome.error is not None and "InferenceBusy" in outcome.error
    assert (env.user_root / "old-extract").is_dir()


async def test_timed_out_review_is_reported(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(
        config=CuratorConfig(llm_review_timeout_seconds=0.05),
        view=view,
        inference=StubInference(delay=1.0),
    )
    assert outcome.error is not None and "timed out" in outcome.error


async def test_a_failed_request_is_reported_as_llm_error(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(
        config=env.config, view=view, inference=StubInference(error=InferenceUnavailable("nope"))
    )
    assert outcome.error is not None and "InferenceUnavailable" in outcome.error


async def test_a_good_answer_is_planned_from_the_model_contract(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    inference = StubInference()
    outcome = await run_review(config=env.config, view=view, inference=inference)
    assert outcome.error is None
    assert outcome.model == "stub-model"
    assert [plan.name for plan in outcome.consolidations] == ["old-extract"]
    assert [plan.name for plan in outcome.prunings] == ["dead-skill"]
    request = inference.requests[0]
    assert request.purpose == "curator_review"
    assert request.max_output_tokens == env.config.llm_review_max_output_tokens
    assert "name=old-extract" in request.prompt
    assert "digest=" in request.prompt
    # The model sees metadata only: the Skill body is never part of the request, so a
    # model answer can never be mistaken for content that was read.
    assert "Extract text from PDFs." not in request.prompt


async def test_consolidation_becomes_a_candidate_and_never_edits_the_skill(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    before = env.manager.main_content("user", "document-tools")
    outcome = await run_review(config=env.config, view=view, inference=StubInference())
    evolution = make_evolution(env, tmp_path)
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=evolution,
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
    )

    assert len(application.candidates) == 1
    candidate = evolution.candidates.get(application.candidates[0]["candidate_id"])
    assert candidate is not None
    assert candidate.scope == "user" and candidate.name == "document-tools"
    assert candidate.status == "cold"
    assert "## Absorbed: old-extract" in evolution.candidates.content(candidate)
    assert application.consolidated[0]["evidence"] == "absorbed"

    # The formal Skill body is byte-for-byte unchanged: the change is a candidate.
    assert env.manager.main_content("user", "document-tools") == before
    assert len(evolution.candidates.list()) == 1
    assert env.library.archived_names("user") == ("dead-skill",)


async def test_an_unknown_umbrella_is_dropped_not_created(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-extract", body="Extract text from PDFs.")
    view = env.library.view()
    outcome = await run_review(config=env.config, view=view, inference=StubInference())
    # Nothing mentions the source and the declared umbrella does not exist, so the entry
    # is dropped: the review never creates a new Skill.
    assert outcome.consolidations == ()
    assert any("does not exist" in item["reason"] for item in outcome.skipped)


async def test_pruning_archives_instead_of_deleting(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(config=env.config, view=view, inference=StubInference())
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=make_evolution(env, tmp_path),
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
    )
    assert [item["name"] for item in application.pruned] == ["dead-skill"]
    assert application.pruned[0]["archived"] is True
    assert not (env.user_root / "dead-skill").exists()
    archived = env.user_root / ".archive" / "dead-skill" / "SKILL.md"
    assert archived.is_file()
    assert "Nothing depends on this." in archived.read_text(encoding="utf-8")
    entry = env.manager.ledger["user"].entries(skill="dead-skill")[0]
    assert entry.action == "archive" and "review pruning" in entry.evidence["reason"]


async def test_protected_prunings_are_refused_with_a_reason(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "user-owned", owner="user")
    view = env.library.view()
    answer = "```yaml\nprunings:\n  - name: user-owned\n    reason: stale\n```"
    outcome = await run_review(config=env.config, view=view, inference=StubInference(answer))
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=make_evolution(env, tmp_path),
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
    )
    assert application.pruned == ()
    assert "user-written skill is not auto-archived" in application.skipped[0]["reason"]
    assert (env.user_root / "user-owned").is_dir()


async def test_prunings_can_be_reported_without_archiving(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(config=env.config, view=view, inference=StubInference())
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=make_evolution(env, tmp_path),
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
        archive_prunings=False,
    )
    assert application.pruned[0]["archived"] is False
    assert (env.user_root / "dead-skill").is_dir()


async def test_a_candidate_needs_a_source_run(tmp_path):
    env = make_env(tmp_path)
    view = seed_library(env)
    outcome = await run_review(config=env.config, view=view, inference=StubInference())
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=make_evolution(env, tmp_path),
        project_enabled=True,
        source_session="session-1",
        source_run=None,
    )
    assert application.candidates == ()
    assert any("needs a source run" in item["reason"] for item in application.skipped)


async def test_a_user_owned_target_refuses_the_candidate(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-extract", body="Extract text.")
    write_skill(
        env.user_root,
        "hand-written",
        body="Hand written umbrella; see old-extract.",
        owner="user",
    )
    answer = (
        "```yaml\nconsolidations:\n  - from: old-extract\n    into: hand-written\n"
        "    reason: r\n```"
    )
    view = env.library.view()
    outcome = await run_review(config=env.config, view=view, inference=StubInference(answer))
    evolution = make_evolution(env, tmp_path)
    application = await apply_review(
        outcome=outcome,
        library=env.library,
        evolution=evolution,
        project_enabled=True,
        source_session="session-1",
        source_run="run-1",
    )
    assert application.candidates == ()
    assert "user-owned" in application.skipped[0]["reason"]
    assert evolution.candidates.list() == []


def test_run_payload_and_report_fields(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    before = env.library.records()
    env.store.set_state("user", "deploy", "stale", since=NOW)
    after = env.library.records()
    payload = build_run_payload(
        run_id="20260501-120000",
        started_at=datetime(2026, 5, 1, 12, 0, tzinfo=NOW.tzinfo),
        duration_seconds=1.234,
        dry_run=False,
        config=env.config,
        before=before,
        after=after,
        transitions=TransitionResult(checked=1, marked_stale=1),
        snapshot=({"id": "snap-1", "reason": "pre-curator-run"},),
        model="m",
        provider="p",
        llm_summary="summary",
        llm_final="final",
        llm_error=None,
        consolidated=({"name": "a", "into": "b", "reason": "r", "evidence": "absorbed"},),
        pruned=({"name": "c", "archived": True},),
        candidates=({"candidate_id": "cid", "status": "cold", "into": "b"},),
        skipped=({"name": "d", "reason": "why"},),
    )
    assert payload["started_at"] == "2026-05-01T12:00:00+00:00"
    assert payload["auto_transitions"]["marked_stale"] == 1
    assert payload["counts"]["before"] == 1 and payload["counts"]["after"] == 1
    assert payload["state_transitions"] == [
        {"name": "user/deploy", "from": "active", "to": "stale"}
    ]
    assert payload["consolidated"][0]["evidence"] == "absorbed"
    assert payload["counts"]["pruned_this_run"] == 1
    assert payload["policy"]["archive_after_days"] == env.config.archive_after_days

    markdown = render_report_markdown(payload)
    assert HYGIENE_DISCLAIMER in markdown
    assert "does not" not in markdown.split("## Automatic")[0].split(HYGIENE_DISCLAIMER)[1]
    assert "## Automatic transitions (no model)" in markdown
    assert "`a` -> `b`" in markdown
    assert "pre-restore to " not in markdown
    assert "`d`: why" in markdown


def test_dry_run_report_says_so(tmp_path):
    env = make_env(tmp_path)
    payload = build_run_payload(
        run_id="20260501-120000",
        started_at=datetime(2026, 5, 1, 12, 0, tzinfo=NOW.tzinfo),
        duration_seconds=0.0,
        dry_run=True,
        config=env.config,
        before=(),
        after=(),
        transitions=TransitionResult(checked=0, applied=False),
        llm_summary="skipped (dry run)",
        snapshot_error="snapshots are disabled",
    )
    markdown = render_report_markdown(payload)
    assert "**Dry run**" in markdown
    assert "Snapshot failed" in markdown
    assert "- applied: no (dry run)" in markdown
