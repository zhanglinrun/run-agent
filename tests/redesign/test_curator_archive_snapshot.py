"""Archive/restore of one Skill and whole-root snapshots with rollback."""

from __future__ import annotations

import json
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.redesign.test_curator_state import NOW, make_env, skill_text, write_skill

from run_agent_extensions.curator import (
    apply_automatic_transitions,
    list_snapshots,
    prune_old,
    resolve_snapshot,
    restore,
    snapshot_skills,
)
from run_agent_extensions.curator import snapshot as snapshot_module


def archive_one(env, name, *, age_days=200):
    write_skill(env.user_root, name, age_days=age_days)
    result = apply_automatic_transitions(
        env.library.records(), now=NOW, config=env.config, library=env.library
    )
    assert result.archived == 1
    return result


def test_archive_moves_the_whole_directory_and_ledger_records_the_move(tmp_path):
    env = make_env(tmp_path)
    directory = write_skill(env.user_root, "old-one", age_days=200)
    (directory / "references").mkdir()
    (directory / "references" / "notes.md").write_text("detail", encoding="utf-8")
    archive_one(env, "old-one")

    moved = env.user_root / ".archive" / "old-one"
    assert (moved / "SKILL.md").is_file()
    assert (moved / "references" / "notes.md").read_text(encoding="utf-8") == "detail"
    assert not directory.exists()
    assert env.library.records() == ()
    entry = env.manager.ledger["user"].entries(skill="old-one")[0]
    assert entry.action == "archive"
    assert entry.actor == "evolution"
    assert {Path(item["path"]).name for item in entry.before} == {"SKILL.md", "notes.md"}
    assert {Path(item["path"]).name for item in entry.after} == {"SKILL.md", "notes.md"}


def test_archive_disambiguates_with_a_timestamp(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-one", age_days=200)
    archive_one(env, "old-one")
    assert (env.user_root / ".archive" / "old-one").is_dir()

    write_skill(env.user_root, "old-one", age_days=200)
    archive_one(env, "old-one")
    names = sorted(item.name for item in (env.user_root / ".archive").iterdir())
    assert len(names) == 2
    assert names[0] == "old-one"
    assert names[1].startswith("old-one-")
    assert names[1].removeprefix("old-one-").isdigit()
    assert len(names[1].removeprefix("old-one-")) == 14


def test_restore_moves_an_archived_skill_back(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-one", age_days=200)
    archive_one(env, "old-one")

    mutation = env.library.restore("user", "old-one")
    assert mutation.ok is True
    assert (env.user_root / "old-one" / "SKILL.md").is_file()
    assert not (env.user_root / ".archive" / "old-one").exists()
    assert [record.name for record in env.library.records()] == ["old-one"]
    assert env.library.records()[0].state == "active"
    entry = env.manager.ledger["user"].entries(skill="old-one")[0]
    assert entry.action == "restore"
    assert entry.actor == "evolution"


def test_restore_refuses_a_missing_or_colliding_name(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "old-one", age_days=200)
    archive_one(env, "old-one")
    write_skill(env.user_root, "old-one")

    collision = env.library.restore("user", "old-one")
    assert collision.ok is False and "already exists" in collision.message
    missing = env.library.restore("user", "never-existed")
    assert missing.ok is False and "no archived skill" in missing.message
    assert env.library.archived_names("user") == ("old-one",)


def test_restore_does_not_take_a_sibling_out_of_the_archive(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "git-helpers", age_days=200)
    archive_one(env, "git-helpers")
    mutation = env.library.restore("user", "git")
    assert mutation.ok is False
    assert (env.user_root / ".archive" / "git-helpers").is_dir()


def test_snapshot_manifest_and_contents(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    (env.user_root / ".archive" / "gone").mkdir(parents=True)
    (env.user_root / ".archive" / "gone" / "SKILL.md").write_text(
        skill_text("gone"), encoding="utf-8"
    )
    env.manager.ledger["user"].append("publish", "deploy", actor="evolution")

    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="unit-test", state_dir=state_dir)
    assert ref is not None
    manifest = json.loads((ref.path / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) >= {
        "id",
        "reason",
        "created_at",
        "archive",
        "archive_bytes",
        "skill_files",
    }
    assert manifest["id"] == ref.id
    assert manifest["reason"] == "unit-test"
    assert manifest["archive"] == "skills.tar.gz"
    assert manifest["archive_bytes"] > 0
    assert manifest["skill_files"] == 2
    assert manifest["root"] == str(env.user_root)

    with tarfile.open(ref.path / "skills.tar.gz") as handle:
        names = set(handle.getnames())
    assert "deploy/SKILL.md" in names
    assert ".archive/gone/SKILL.md" in names
    assert ".ledger.jsonl" in names


def test_snapshot_skips_its_own_bookkeeping(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    (env.user_root / ".curator_backups").mkdir()
    (env.user_root / ".curator_backups" / "old.tar.gz").write_bytes(b"x")
    (env.user_root / ".write.lock").write_text("0", encoding="ascii")
    (env.user_root / ".rollback-staging-keep").mkdir()
    ref = snapshot_skills(env.user_root, reason="skip", state_dir=env.home / "state" / "extensions")
    assert ref is not None
    with tarfile.open(ref.path / "skills.tar.gz") as handle:
        names = set(handle.getnames())
    assert names == {"deploy", "deploy/SKILL.md"}


def test_snapshot_of_a_missing_root_is_none(tmp_path):
    env = make_env(tmp_path)
    assert (
        snapshot_skills(
            env.user_root, reason="missing", state_dir=env.home / "state" / "extensions"
        )
        is None
    )


def test_prune_keeps_the_newest_and_protects(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    ids = []
    for index in range(4):
        ref = snapshot_skills(
            env.user_root,
            reason=f"run-{index}",
            state_dir=state_dir,
            keep=10,
            now=datetime(2026, 5, 1, 12, 0, index, tzinfo=UTC),
        )
        assert ref is not None
        ids.append(ref.id)
    deleted = prune_old(state_dir, keep=2, protect={ids[0]})
    assert deleted == (ids[1],)
    remaining = [item.id for item in list_snapshots(state_dir)]
    assert remaining == sorted({ids[0], ids[2], ids[3]}, reverse=True)
    assert resolve_snapshot(state_dir, ids[1]) is None
    assert resolve_snapshot(state_dir).id == ids[3]


def test_snapshot_skills_prunes_automatically(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    for index in range(4):
        snapshot_skills(
            env.user_root,
            reason=f"run-{index}",
            state_dir=state_dir,
            keep=2,
            now=datetime(2026, 5, 1, 12, 0, index, tzinfo=UTC),
        )
    assert len(list_snapshots(state_dir)) == 2


def test_restore_replaces_the_tree_and_keeps_a_pre_restore_snapshot(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", body="original body")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None

    write_skill(env.user_root, "deploy", body="edited later")
    write_skill(env.user_root, "added-later")

    entered: list[str] = []

    @contextmanager
    def lock():
        entered.append("held")
        yield

    ok, message = restore(ref.id, root=env.user_root, state_dir=state_dir, lock=lock)
    assert ok is True, message
    assert entered == ["held"]
    assert "original body" in (env.user_root / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert not (env.user_root / "added-later").exists()
    assert not list(env.user_root.glob(".rollback-staging-*"))
    snapshots = list_snapshots(state_dir)
    assert snapshots[0].reason.startswith("pre-restore to ")
    assert ref.id in {item.id for item in snapshots}
    assert "pre-restore snapshot" in message


def test_restore_is_idempotent(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None
    first, first_message = restore(ref.id, root=env.user_root, state_dir=state_dir)
    second, second_message = restore(ref.id, root=env.user_root, state_dir=state_dir)
    assert (first, second) == (True, True), (first_message, second_message)
    assert (env.user_root / "deploy" / "SKILL.md").is_file()
    assert not list(env.user_root.glob(".rollback-staging-*"))


def test_restore_refuses_an_unknown_snapshot(tmp_path):
    env = make_env(tmp_path)
    ok, message = restore("nope", root=env.user_root, state_dir=env.home / "state" / "extensions")
    assert ok is False and "no snapshot with id" in message


@pytest.mark.parametrize("member", ["../escape.txt", "/absolute.txt", "sub/../../escape.txt"])
def test_restore_rejects_unsafe_members_and_changes_nothing(tmp_path, member):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", body="kept")
    state_dir = env.home / "state" / "extensions"
    snapshot_id = "2026-05-01T00-00-00Z"
    directory = snapshot_module.backups_dir(state_dir) / snapshot_id
    directory.mkdir(parents=True)
    archive = directory / "skills.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = skill_text("evil").encode("utf-8")
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        import io

        handle.addfile(info, io.BytesIO(payload))
    (directory / "manifest.json").write_text(
        json.dumps({"id": snapshot_id, "root": str(env.user_root)}), encoding="utf-8"
    )

    ok, message = restore(snapshot_id, root=env.user_root, state_dir=state_dir)
    assert ok is False
    assert "unsafe archive member" in message
    assert "kept" in (env.user_root / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert [item.name for item in env.user_root.iterdir()] == ["deploy"]
    assert not (env.user_root.parent / "escape.txt").exists()


def test_restore_rejects_a_symlink_member(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    snapshot_id = "2026-05-01T00-00-00Z"
    directory = snapshot_module.backups_dir(state_dir) / snapshot_id
    directory.mkdir(parents=True)
    with tarfile.open(directory / "skills.tar.gz", "w:gz") as handle:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        handle.addfile(info)
    (directory / "manifest.json").write_text(
        json.dumps({"id": snapshot_id, "root": str(env.user_root)}), encoding="utf-8"
    )
    ok, message = restore(snapshot_id, root=env.user_root, state_dir=state_dir)
    assert ok is False and "unsafe archive member" in message
    assert (env.user_root / "deploy").is_dir()


def test_failed_extract_puts_the_previous_tree_back(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", body="original")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None
    archive = ref.path / "skills.tar.gz"
    archive.write_bytes(archive.read_bytes()[:20])

    write_skill(env.user_root, "deploy", body="newer")
    write_skill(env.user_root, "only-here")

    ok, message = restore(ref.id, root=env.user_root, state_dir=state_dir)
    assert ok is False
    assert "the previous tree was put back" in message
    assert "newer" in (env.user_root / "deploy" / "SKILL.md").read_text(encoding="utf-8")
    assert (env.user_root / "only-here").is_dir()
    assert not list(env.user_root.glob(".rollback-staging-*"))


def test_a_failed_move_back_keeps_the_staging_directory(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None
    (ref.path / "skills.tar.gz").write_bytes(b"not a tarball")

    monkeypatch.setattr(snapshot_module, "_unstage", lambda moved: ["deploy"])
    ok, message = restore(ref.id, root=env.user_root, state_dir=state_dir)
    assert ok is False
    assert "could not move back deploy" in message
    assert "staged copies kept at" in message
    staged = list(env.user_root.glob(".rollback-staging-*"))
    assert len(staged) == 1
    assert (staged[0] / "deploy" / "SKILL.md").is_file()


def test_restore_without_a_lock_still_works(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None
    ok, _ = restore(ref.id, root=env.user_root, state_dir=state_dir, lock=None)
    assert ok is True
    assert (env.user_root / "deploy" / "SKILL.md").is_file()


def test_restore_into_a_missing_root_creates_it(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    state_dir = env.home / "state" / "extensions"
    ref = snapshot_skills(env.user_root, reason="before", state_dir=state_dir)
    assert ref is not None
    other = tmp_path / "restored-skills"
    ok, _ = restore(ref.id, root=other, state_dir=state_dir)
    assert ok is True
    assert (other / "deploy" / "SKILL.md").is_file()
