"""Backups copy session trees and preserve v2 restore compatibility."""

import hashlib
import json
from pathlib import Path

import pytest

from run_agent_coding.storage.backup import create_backup, restore_backup, verify_backup


def _write_tree(home: Path) -> None:
    session_dir = home / "sessions" / "demo-abc123"
    session_dir.mkdir(parents=True)
    (session_dir / "index.jsonl").write_text(
        '{"id":"s1","path":"s1.jsonl","cwd":".","model":"test","created_at":1,"updated_at":1}\n',
        encoding="utf-8",
    )
    (session_dir / "s1.jsonl").write_text('{"type":"message","id":"a"}\n', encoding="utf-8")
    legacy = home / "gateway"
    legacy.mkdir()
    (legacy / "sessions.jsonl").write_text(
        '{"session_key":"chat","session_id":"s1"}\n', encoding="utf-8"
    )
    (legacy / "deliveries.jsonl").write_text(
        '{"obligation_id":"o1","status":"pending"}\n', encoding="utf-8"
    )


def _add_legacy_files_to_v2_manifest(backup: Path, home: Path) -> None:
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"] = "run.backup.v2"
    for relative in ("gateway/sessions.jsonl", "gateway/deliveries.jsonl"):
        payload = (home / relative).read_bytes()
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        manifest["files"].append(
            {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        )
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")


async def test_backup_v3_restores_only_sessions(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    destination = await create_backup(home, tmp_path / "backup")
    manifest = await verify_backup(destination)
    assert manifest["schema"] == "run.backup.v3"
    assert {item["path"] for item in manifest["files"]} == {
        "sessions/demo-abc123/index.jsonl",
        "sessions/demo-abc123/s1.jsonl",
    }
    assert not (destination / "gateway").exists()
    (home / "sessions" / "demo-abc123" / "s1.jsonl").write_text(
        '{"type":"message","id":"after"}\n', encoding="utf-8"
    )
    restored = await restore_backup(destination, tmp_path / "restored")
    assert (
        (restored / "sessions/demo-abc123/s1.jsonl")
        .read_text(encoding="utf-8")
        .startswith('{"type":"message","id":"a"}')
    )
    assert not (restored / "gateway").exists()


async def test_verify_and_restore_accept_v2_manifests_with_legacy_files(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    backup = await create_backup(home, tmp_path / "backup")
    _add_legacy_files_to_v2_manifest(backup, home)

    manifest = await verify_backup(backup)
    assert manifest["schema"] == "run.backup.v2"
    restored = await restore_backup(backup, tmp_path / "restored")
    assert (restored / "sessions/demo-abc123/s1.jsonl").is_file()
    assert (restored / "gateway/sessions.jsonl").is_file()
    assert (restored / "gateway/deliveries.jsonl").is_file()


async def test_missing_file_fails_backup_without_publishing_partial_package(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    (home / "sessions" / "demo-abc123" / "s1.jsonl").unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        await create_backup(home, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    empty = tmp_path / "empty-home"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        await create_backup(empty, tmp_path / "backup2")
    assert not (tmp_path / "backup2").exists()


async def test_tampered_backup_fails_before_creating_restore_target(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    backup = await create_backup(home, tmp_path / "backup")
    target = backup / "sessions/demo-abc123/s1.jsonl"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        await restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


async def test_restore_does_not_replace_existing_directory_or_remove_legacy_state(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    backup = await create_backup(home, tmp_path / "backup")
    target = tmp_path / "existing"
    legacy = target / "gateway" / "keep"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("existing state")
    with pytest.raises(FileExistsError):
        await restore_backup(backup, target)
    assert legacy.read_text() == "existing state"
