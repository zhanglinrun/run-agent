"""The curator and the review trigger: ageing, consolidation plans, cadence and config."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from run_agent_coding.host.learning import review_origin, writeback_disabled
from run_agent_extensions.experience.config import ExperienceConfig, load_experience_config
from run_agent_extensions.experience.curator import Curator, CuratorLease
from run_agent_extensions.experience.review import ReviewTrigger
from run_agent_extensions.experience.review_models import (
    ReviewPolicy,
    ReviewRequest,
    looks_like_correction,
    summarize_edits,
)
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots
from run_agent_extensions.experience.skill_usage import STATE_ARCHIVED, STATE_STALE

BODY = "# Deploy\n\n## When to Use\n- deploying\n\n## Procedure\n1. Run the tests.\n\n## Pitfalls\n- none\n"


@pytest.fixture
def manager(tmp_path):
    (tmp_path / "user").mkdir()
    (tmp_path / "project").mkdir()
    return SkillManager(SkillRoots(user=tmp_path / "user", project=tmp_path / "project"))


# --- curator ------------------------------------------------------------------------


def make_curator(manager, tmp_path, *, now):
    cfg = ExperienceConfig(
        curator_stale_after_days=30, curator_archive_after_days=90, curator_interval_hours=1
    )
    return Curator(manager, cfg, tmp_path / "state", clock=lambda: now[0])


def test_curator_ages_managed_skills_and_skips_pinned_ones(manager, tmp_path):
    now = [datetime(2026, 9, 12, tzinfo=UTC).timestamp()]
    with review_origin():
        for name in ("old", "fresh", "pinned", "never"):
            manager.create("project", name, f"Do {name} things well.", BODY)
    manager.create("project", "mine", "User-owned skill here.", BODY)
    usage = manager.usage["project"]
    usage.set_pinned("pinned", True)
    ancient = (datetime.fromtimestamp(now[0], tz=UTC) - timedelta(days=120)).isoformat()
    for name in ("old", "pinned"):
        usage._mutate(name, lambda r: r.update({"last_used_at": ancient, "use_count": 3}))
    usage._mutate(
        "fresh",
        lambda r: r.update(
            {"last_used_at": datetime.fromtimestamp(now[0], tz=UTC).isoformat(), "use_count": 1}
        ),
    )
    usage._mutate("never", lambda r: r.update({"created_at": ancient}))
    curator = make_curator(manager, tmp_path, now=now)
    counts = curator.apply_transitions()
    assert counts == {"checked": 4, "marked_stale": 0, "archived": 2, "reactivated": 0}
    assert usage.get("old")["state"] == STATE_ARCHIVED
    assert usage.get("never")["state"] == STATE_ARCHIVED  # never used but far past the window
    assert usage.get("pinned")["state"] == "active" and (tmp_path / "project" / "pinned").is_dir()
    assert (tmp_path / "project" / "mine").is_dir()  # user-owned: not even considered
    # Idle past the stale window but inside the archive window: stale, then reactivated.
    forty = (datetime.fromtimestamp(now[0], tz=UTC) - timedelta(days=40)).isoformat()
    usage._mutate("fresh", lambda r: r.update({"last_used_at": forty}))
    assert curator.apply_transitions()["marked_stale"] == 1
    assert usage.get("fresh")["state"] == STATE_STALE
    usage.bump_use("fresh")
    assert curator.apply_transitions()["reactivated"] == 1


async def test_curator_gates_on_interval_and_idle_and_applies_a_consolidation_plan(
    manager, tmp_path
):
    now = [datetime(2026, 9, 12, tzinfo=UTC).timestamp()]
    curator = make_curator(manager, tmp_path, now=now)
    assert not curator.should_run(idle_seconds=10_000)  # first sighting seeds the clock
    now[0] += 3600 * 2
    assert not curator.should_run(idle_seconds=10)  # not idle enough
    assert curator.should_run(idle_seconds=10_000)
    with review_origin():
        manager.create("project", "deploy-fri", "Deploy on fridays.", BODY)
        manager.create("project", "deploy-mon", "Deploy on mondays.", BODY)
    plan = {
        "patches": [],
        "creates": [
            {
                "scope": "project",
                "name": "deploy",
                "description": "Deploy on any weekday.",
                "body": BODY,
            }
        ],
        "support_files": [
            {
                "scope": "project",
                "name": "deploy",
                "file_path": "references/fridays.md",
                "content": "- friday notes\n",
            }
        ],
        "archives": [
            {
                "scope": "project",
                "name": "deploy-fri",
                "absorbed_into": "deploy",
                "reason": "merged",
            },
            {"scope": "project", "name": "deploy-mon", "absorbed_into": "missing", "reason": "bad"},
        ],
        "keep": [],
    }
    prompts = []

    async def ask(system, prompt):
        prompts.append(prompt)
        # Maintenance reads must not make idle skills look recently used.
        for name in ("deploy-fri", "deploy-mon"):
            record = manager.usage["project"].get(name)
            assert record["view_count"] == 0 and record["last_viewed_at"] is None
        return json.dumps(plan)

    run = await curator.run(ask, consolidate=True)
    assert "deploy-fri" in prompts[0] and "SKILL.md:" in prompts[0]
    assert len(run.consolidation["applied"]) == 3 and len(run.consolidation["refused"]) == 1
    assert (tmp_path / "project" / "deploy" / "references" / "fridays.md").is_file()
    assert (tmp_path / "project" / ".archive" / "deploy-fri").is_dir()
    assert (tmp_path / "project" / "deploy-mon").is_dir()
    assert run.report_path and "refused" in run.report_path.read_text(encoding="utf-8")
    assert curator.state.run_count == 1 and not curator.should_run(idle_seconds=10_000)
    dry = await curator.run(ask, dry_run=True, consolidate=True)
    assert dry.consolidation["skipped"] == "dry run" and curator.state.run_count == 1
    assert (
        "deploy" in curator.status_text()
        and "archived: project/deploy-fri" in curator.status_text()
    )


def test_curator_handles_categorized_skills_and_evaluation_does_not_count_usage(manager, tmp_path):
    now = [datetime(2026, 9, 12, tzinfo=UTC).timestamp()]
    with review_origin():
        manager.create("project", "nested", "A nested skill.", BODY, category="ops")
    usage = manager.usage["project"]
    ancient = (datetime.fromtimestamp(now[0], tz=UTC) - timedelta(days=120)).isoformat()
    usage._mutate("nested", lambda record: record.update({"last_used_at": ancient, "use_count": 2}))
    curator = make_curator(manager, tmp_path, now=now)
    assert curator.apply_transitions()["archived"] == 1
    assert (tmp_path / "project" / ".archive" / "nested").is_dir()

    with writeback_disabled():
        assert not curator.should_run(idle_seconds=10_000)
    assert not (tmp_path / "state" / ".curator_state.json").exists()

    with review_origin():
        manager.create("project", "measured", "Measured skill.", BODY)
    with writeback_disabled():
        viewed = manager.view("project", "measured")
        assert "# Deploy" in viewed
        manager.record_use("project", "measured")
    record = manager.usage["project"].get("measured")
    assert record["view_count"] == 0 and record["use_count"] == 0


def test_curator_uses_one_root_lease(tmp_path):
    path = tmp_path / "experience" / ".curator.lock"
    first = CuratorLease(path)
    second = CuratorLease(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    assert not path.exists()


async def test_curator_refuses_autonomous_writes_when_approval_is_required(manager, tmp_path):
    config = ExperienceConfig(skills_write_approval=True)
    curator = Curator(manager, config, tmp_path / "state")
    run = await curator.run(None)
    assert run.consolidation["skipped"] == "skill write requires explicit approval"


def test_review_cadence_and_corrections_admit_a_review():
    trigger = ReviewTrigger(
        policy=ReviewPolicy(review_every_turns=3, cooldown_seconds=0, review_on_signals=True)
    )
    base = dict(
        source_run_id="r",
        session_id="s",
        status="succeeded",
        assistant_turns=2,
        corrections=0,
        failures=0,
    )
    assert not trigger.consider(ReviewRequest(**base, runs_since_review=1)).admitted
    assert trigger.consider(
        ReviewRequest(**{**base, "source_run_id": "r2"}, runs_since_review=3)
    ).admitted
    assert trigger.consider(
        ReviewRequest(**{**base, "source_run_id": "r3", "corrections": 1, "assistant_turns": 1})
    ).admitted
    assert looks_like_correction("stop doing that, just give me the answer")
    assert looks_like_correction("不要再解释了，直接给我结果")
    assert not looks_like_correction("please add a test for the parser")
    assert summarize_edits(
        ["user/user: Added", "skill project/x: patch"], ["bad"], verbose=False
    ) == ("review: memory updated (1), skills updated (1), 1 refused")


def test_experience_config_reads_the_environment():
    cfg = load_experience_config(
        {
            "EXPERIENCE_MEMORY_CHAR_LIMIT": "3000",
            "EXPERIENCE_REVIEW_EVERY_TURNS": "5",
            "EXPERIENCE_CURATOR_CONSOLIDATE": "true",
            "EXPERIENCE_REVIEW_NOTIFY": "verbose",
        }
    )
    assert cfg.memory_char_limit == 3000 and cfg.review_every_turns == 5
    assert cfg.curator_consolidate and cfg.review_notify == "verbose"
    assert load_experience_config({}).review_on_signals is False
    with pytest.raises(ValueError):
        load_experience_config(
            {"EXPERIENCE_CURATOR_ARCHIVE_DAYS": "5", "EXPERIENCE_CURATOR_STALE_DAYS": "30"}
        )
