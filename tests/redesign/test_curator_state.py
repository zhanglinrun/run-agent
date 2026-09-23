"""Curator policy, durable state and consultation log.

Also hosts the small helpers every other ``test_curator_*`` module imports, so the
fixtures stay identical across the Curator test set.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from run_agent_extensions.curator import (
    CuratorConfig,
    CuratorLibrary,
    CuratorState,
    CuratorStateStore,
    load_curator_config,
)
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots

# The tests measure inactivity against the same clock the Skill files were stamped
# with, so "45 days old" means 45 days before this run rather than before a fixed date.
NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass(slots=True)
class CuratorEnv:
    """One isolated Curator environment rooted in ``tmp_path``."""

    home: Path
    project: Path
    manager: SkillManager
    store: CuratorStateStore
    library: CuratorLibrary
    config: CuratorConfig

    @property
    def user_root(self) -> Path:
        return self.home / "skills"

    @property
    def project_root(self) -> Path:
        return self.project / ".run" / "skills"


def skill_text(
    name: str, body: str = "Run the deploy and check the logs.", *, owner: str = "evolution"
) -> str:
    """Return a valid ``SKILL.md`` body for ``name``."""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Work with {name} safely.\n"
        f"created_by: {owner}\n"
        "---\n\n"
        f"# {name.replace('-', ' ').title()}\n\n"
        "## Procedure\n"
        f"{body}\n"
    )


def write_skill(
    root: Path,
    name: str,
    *,
    body: str = "Run the deploy and check the logs.",
    owner: str = "evolution",
    age_days: float | None = None,
) -> Path:
    """Create one Skill package, optionally with an old ``SKILL.md`` mtime."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(skill_text(name, body, owner=owner), encoding="utf-8")
    if age_days is not None:
        stamp = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
        os.utime(path, (stamp, stamp))
    return directory


def make_env(
    tmp_path: Path,
    *,
    config: CuratorConfig | None = None,
    project_enabled: bool = True,
    now: datetime | None = NOW,
) -> CuratorEnv:
    """Build a Curator environment whose state lives outside both Skill roots."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    chosen = config or CuratorConfig()
    manager = SkillManager(
        SkillRoots(home / "skills", project / ".run" / "skills"), guard=False, ledger=True
    )
    store = CuratorStateStore(home / "state" / "extensions")
    library = CuratorLibrary(
        manager, store, config=chosen, project_enabled=project_enabled, now=now
    )
    return CuratorEnv(home, project, manager, store, library, chosen)


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("CURATOR_ENABLED", "off", False),
        ("CURATOR_INTERVAL_HOURS", "24", 24.0),
        ("CURATOR_STALE_AFTER_DAYS", "7", 7.0),
        ("CURATOR_ARCHIVE_AFTER_DAYS", "120", 120.0),
        ("CURATOR_AUTO_ARCHIVE_USER_SKILLS", "true", True),
        ("CURATOR_AUTO_ARCHIVE_PROJECT_SKILLS", "yes", True),
        ("CURATOR_BACKUP_ENABLED", "0", False),
        ("CURATOR_BACKUP_KEEP", "2", 2),
        ("CURATOR_LLM_REVIEW_ENABLED", "no", False),
        ("CURATOR_LLM_REVIEW_MAX_OUTPUT_TOKENS", "64", 64),
        ("CURATOR_LLM_REVIEW_TIMEOUT_SECONDS", "5", 5.0),
        ("CURATOR_USAGE_LOOKBACK_DAYS", "3", 3.0),
    ],
)
def test_every_curator_variable_is_read(variable, value, expected):
    config = load_curator_config({variable: value})
    field = variable.removeprefix("CURATOR_").lower()
    assert getattr(config, field) == expected


def test_defaults_are_the_conservative_ones():
    config = load_curator_config({})
    assert config.enabled is True
    assert config.interval_hours == 168.0
    assert config.stale_after_days == 30.0
    assert config.archive_after_days == 90.0
    assert config.auto_archive_user_skills is False
    assert config.auto_archive_project_skills is False
    assert config.backup_enabled is True
    assert config.backup_keep == 5
    assert config.llm_review_enabled is True
    assert config.usage_lookback_days == 30.0
    assert config.protected_skill_names == ()
    assert config.protected_names == frozenset()


def test_protected_names_are_split_and_deduplicated():
    config = load_curator_config({"CURATOR_PROTECTED_SKILL_NAMES": "plan, plan, release"})
    assert config.protected_skill_names == ("plan", "release")


@pytest.mark.parametrize(
    "values",
    [
        {"CURATOR_INTERVAL_HOURS": "0"},
        {"CURATOR_STALE_AFTER_DAYS": "not-a-number"},
        {"CURATOR_ARCHIVE_AFTER_DAYS": "1", "CURATOR_STALE_AFTER_DAYS": "30"},
        {"CURATOR_BACKUP_KEEP": "0"},
        {"CURATOR_LLM_REVIEW_MAX_OUTPUT_TOKENS": "0"},
        {"CURATOR_LLM_REVIEW_TIMEOUT_SECONDS": "0"},
        {"CURATOR_USAGE_LOOKBACK_DAYS": "0"},
        {"CURATOR_ENABLED": "maybe"},
    ],
)
def test_unusable_policy_is_refused(values):
    with pytest.raises(ValueError):
        load_curator_config(values)


def test_state_round_trips_through_json(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    state = CuratorState(
        last_run_at="2026-05-01T00:00:00+00:00",
        last_run_duration_seconds=1.5,
        last_run_summary="auto: 1 archived",
        last_run_summary_shown_at="2026-05-01T00:00:01+00:00",
        last_report_path=str(store.report_dir("20260501-000000")),
        paused=True,
        run_count=3,
        last_review_run_id="run-7",
        last_review_run_at="2026-05-01T00:00:02+00:00",
    )
    state.set_record("user", "deploy", "stale", since="2026-04-01T00:00:00+00:00")
    assert store.save(state) is True

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["records"]["user/deploy"] == {
        "state": "stale",
        "since": "2026-04-01T00:00:00+00:00",
    }
    loaded = store.load()
    assert loaded.as_json() == state.as_json()
    assert loaded.record("user", "deploy").state == "stale"
    assert loaded.record("project", "deploy").state == "active"
    assert loaded.records_in("stale") == {"user/deploy": loaded.record("user", "deploy")}


def test_state_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    state = store.load()
    state.run_count = 2
    store.save(state)
    leftovers = [item.name for item in store.root.iterdir() if item.name.startswith(".state.json.")]
    assert leftovers == []
    assert json.loads(store.path.read_text(encoding="utf-8"))["run_count"] == 2


def test_damaged_state_reads_as_defaults(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load().as_json() == CuratorState().as_json()


def test_update_refuses_an_unknown_field(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    with pytest.raises(ValueError, match="Unknown Curator state field"):
        store.update(nonsense=1)


def test_usage_log_is_append_only_and_lookback_bounded(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    old = datetime.now(UTC) - timedelta(days=10)
    assert store.record_usage(
        skill="deploy", scope="user", source="slash_command", consulted_at=old
    )
    assert store.record_usage(skill="deploy", scope="user", source="skill_tool")
    assert store.record_usage(skill="deploy", scope="user", source="turn")
    assert store.record_usage(skill="deploy", scope="user", source="not-a-source") is False

    lines = store.usage_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert {json.loads(line)["source"] for line in lines} == {
        "slash_command",
        "skill_tool",
        "turn",
    }
    recent = store.last_consulted_at("deploy", "user", not_before=old.timestamp() + 1)
    assert recent is not None and recent > old.timestamp() + 1
    assert store.last_consulted_at("deploy", "user", not_before=recent + 1) is None
    assert store.last_consulted_at("other", "user") is None


def test_usage_skips_malformed_lines(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    store.record_usage(skill="deploy", scope="user", source="turn")
    with open(store.usage_path, "a", encoding="utf-8") as stream:
        stream.write("{oops\n")
    assert [record.skill for record in store.usage()] == ["deploy"]


def test_reports_get_unique_ids_and_read_back(tmp_path):
    store = CuratorStateStore(tmp_path / "state")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    first = store.next_run_id(now)
    assert first == "20260501-120000"
    assert store.write_report(first, {"run_id": first}, "# report\n") is not None
    assert store.next_run_id(now) == "20260501-120000-02"
    assert store.read_report(first) == "# report\n"
    assert store.read_run(first) == {"run_id": first}
    assert store.report_ids() == (first,)
    assert store.latest_report_id() == first
    assert store.read_report("missing") is None


def test_state_for_reads_the_stored_state(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    env.store.set_state("user", "deploy", "stale", since=NOW)
    assert env.library.records()[0].state == "stale"
