"""Skill records, the protection rules and the automatic transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.redesign.test_curator_state import NOW, make_env, write_skill

from run_agent_extensions.curator import CuratorConfig, apply_automatic_transitions


def transition(env, now=NOW, config=None, apply=True):
    return apply_automatic_transitions(
        env.library.records(),
        now=now,
        config=config or env.config,
        library=env.library,
        apply=apply,
    )


def test_records_report_file_state_and_ledger_activity(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", age_days=3)
    records = env.library.records()
    assert [record.key for record in records] == ["user/deploy"]
    record = records[0]
    assert record.name == "deploy"
    assert record.scope == "user"
    assert record.created_by == "evolution"
    assert record.pinned is False
    assert record.state == "active"
    assert len(record.digest) == 64
    assert record.mtime > 0
    assert record.last_mutation_at is None
    assert record.last_consulted_at is None
    assert record.anchor == record.mtime
    assert record.as_json()["scope"] == "user"


def test_a_recent_consultation_moves_the_anchor(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", age_days=45)
    env.store.record_usage(skill="deploy", scope="user", source="slash_command")
    record = env.library.records()[0]
    assert record.last_consulted_at is not None
    assert record.anchor == record.last_consulted_at
    assert record.anchor > record.mtime


def test_a_consultation_outside_the_lookback_window_does_not_count(tmp_path):
    env = make_env(tmp_path, config=CuratorConfig(usage_lookback_days=1))
    write_skill(env.user_root, "deploy", age_days=45)
    env.store.record_usage(
        skill="deploy",
        scope="user",
        source="turn",
        consulted_at=datetime.now(UTC) - timedelta(days=10),
    )
    record = env.library.records()[0]
    assert record.last_consulted_at is None
    assert record.anchor == record.mtime


def test_ledger_entries_become_the_mutation_anchor(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", age_days=200)
    entry = env.manager.ledger["user"].entries(skill="deploy")
    assert entry == []
    # One publish-style ledger write is what the anchor reads.
    env.manager.ledger["user"].append("publish", "deploy", actor="evolution")
    record = env.library.records()[0]
    assert record.last_mutation_at is not None
    assert record.anchor == record.last_mutation_at


def test_project_scope_is_discovered_only_when_trusted(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "user-skill")
    write_skill(env.project_root, "project-skill")
    assert {record.key for record in env.library.records()} == {
        "user/user-skill",
        "project/project-skill",
    }
    untrusted = make_env(tmp_path / "other", project_enabled=False)
    write_skill(untrusted.user_root, "user-skill")
    write_skill(untrusted.project_root, "project-skill")
    assert {record.key for record in untrusted.library.records()} == {"user/user-skill"}


def test_dot_directories_are_never_discovered(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root / ".archive", "archived")
    write_skill(env.user_root, "live")
    assert [record.name for record in env.library.records()] == ["live"]


def test_protection_reasons_in_priority_order(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "evolution-skill")
    write_skill(env.user_root, "user-skill", owner="user")
    write_skill(env.user_root, "no-owner", owner="")
    write_skill(env.project_root, "project-user", owner="user")
    env.manager.usage["user"].set_pinned("evolution-skill", True)
    config = CuratorConfig(protected_skill_names=("no-owner",))
    library = type(env.library)(
        env.manager, env.store, config=config, project_enabled=True, now=NOW
    )
    reasons = {record.key: library.protected(record) for record in library.records()}
    assert reasons["user/evolution-skill"] == "pinned"
    assert reasons["user/no-owner"] == "protected name"
    assert reasons["user/user-skill"] == "user-written skill is not auto-archived"
    assert reasons["project/project-user"] == "project scope is not auto-archived"
    assert library.transition_block(library.records()[0]) == "pinned"


def test_protection_flags_open_the_default_deny_door(tmp_path):
    env = make_env(
        tmp_path,
        config=CuratorConfig(auto_archive_user_skills=True, auto_archive_project_skills=True),
    )
    write_skill(env.user_root, "user-skill", owner="user")
    write_skill(env.project_root, "project-skill", owner="user")
    assert all(env.library.protected(record) is None for record in env.library.records())


def test_project_scope_is_never_archived_while_untrusted(tmp_path):
    env = make_env(tmp_path, project_enabled=True)
    write_skill(env.project_root, "project-skill", age_days=400)
    untrusted = make_env(
        tmp_path, project_enabled=False, config=CuratorConfig(auto_archive_project_skills=True)
    )
    # Rebuild the library in the same tree but with an untrusted project.
    library = type(env.library)(
        env.manager,
        env.store,
        config=untrusted.config,
        project_enabled=False,
        now=NOW,
    )
    record = next(item for item in env.library.records() if item.scope == "project")
    assert library.protected(record) == "project inputs are untrusted"


def test_stale_boundary_days(tmp_path):
    env = make_env(tmp_path, config=CuratorConfig(stale_after_days=30, archive_after_days=90))
    write_skill(env.user_root, "exactly-stale", age_days=30.01)
    write_skill(env.user_root, "just-active", age_days=29.99)
    result = transition(env)
    assert result.checked == 2
    assert result.marked_stale == 1
    assert result.archived == 0
    states = {record.name: record.state for record in env.library.records()}
    assert states == {"exactly-stale": "stale", "just-active": "active"}


def test_archive_boundary_days_and_protection_refusal(tmp_path):
    env = make_env(
        tmp_path,
        config=CuratorConfig(stale_after_days=30, archive_after_days=90),
    )
    write_skill(env.user_root, "exactly-archive", age_days=90.01)
    write_skill(env.user_root, "user-owned", owner="user", age_days=400)
    write_skill(env.project_root, "project-owned", owner="user", age_days=400)
    result = transition(env)
    assert result.archived == 1
    assert (env.user_root / ".archive" / "exactly-archive" / "SKILL.md").is_file()
    reasons = {item["name"]: item["reason"] for item in result.skipped}
    assert reasons["user/user-owned"] == "not archived: user-written skill is not auto-archived"
    assert reasons["project/project-owned"] == "not archived: project scope is not auto-archived"


def test_marking_stale_is_allowed_for_a_user_written_skill(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "user-owned", owner="user", age_days=45)
    result = transition(env)
    assert result.marked_stale == 1
    assert result.archived == 0
    assert env.library.records()[0].state == "stale"


def test_pinned_and_protected_names_are_skipped_entirely(tmp_path):
    env = make_env(tmp_path, config=CuratorConfig(protected_skill_names=("keep-me",)))
    write_skill(env.user_root, "pinned-one", age_days=400)
    write_skill(env.user_root, "keep-me", age_days=400)
    env.manager.usage["user"].set_pinned("pinned-one", True)
    result = transition(env)
    assert result.archived == 0
    assert result.marked_stale == 0
    assert {item["name"]: item["reason"] for item in result.skipped} == {
        "user/pinned-one": "pinned",
        "user/keep-me": "protected name",
    }
    assert (env.user_root / "pinned-one").is_dir()
    assert (env.user_root / "keep-me").is_dir()


def test_reactivation_when_consulted_again(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", age_days=45)
    assert transition(env).marked_stale == 1
    env.store.record_usage(skill="deploy", scope="user", source="slash_command")
    result = transition(env)
    assert result.reactivated == 1
    assert env.library.records()[0].state == "active"


def test_transitions_are_idempotent_for_a_fixed_now(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "stale-one", age_days=45)
    write_skill(env.user_root, "archive-one", age_days=200)
    first = transition(env)
    second = transition(env)
    assert (first.marked_stale, first.archived) == (1, 1)
    assert (second.marked_stale, second.archived, second.reactivated) == (0, 0, 0)
    assert second.checked == 1  # the archived Skill is no longer discovered


def test_dry_run_plan_changes_nothing(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "stale-one", age_days=45)
    write_skill(env.user_root, "archive-one", age_days=200)
    result = transition(env, apply=False)
    assert result.applied is False
    assert (result.marked_stale, result.archived) == (1, 1)
    assert {item["to"] for item in result.planned} == {"stale", "archived"}
    assert {record.name: record.state for record in env.library.records()} == {
        "stale-one": "active",
        "archive-one": "active",
    }
    assert (env.user_root / "archive-one").is_dir()
    assert not (env.user_root / ".archive").exists()


def test_archive_move_is_recorded_before_and_after(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-one", age_days=200)
    assert transition(env).archived == 1
    entry = env.manager.ledger["user"].entries(skill="old-one")[0]
    assert entry.action == "archive"
    assert entry.actor == "evolution"
    assert entry.before and entry.after
    assert entry.evidence["reason"].startswith("inactive for")
    assert entry.evidence["root"] == str(env.user_root)
