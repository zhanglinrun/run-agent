"""Backups copy session trees and gateway JSONL files."""

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
    gateway = home / "gateway"
    gateway.mkdir()
    (gateway / "sessions.jsonl").write_text(
        '{"session_key":"chat","session_id":"s1"}\n', encoding="utf-8"
    )
    (gateway / "deliveries.jsonl").write_text(
        '{"obligation_id":"o1","status":"pending"}\n', encoding="utf-8"
    )


async def test_backup_restores_sessions_and_gateway_jsonl(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    destination = await create_backup(home, tmp_path / "backup")
    manifest = await verify_backup(destination)
    assert {item["path"] for item in manifest["files"]} >= {
        "sessions/demo-abc123/index.jsonl",
        "sessions/demo-abc123/s1.jsonl",
        "gateway/sessions.jsonl",
        "gateway/deliveries.jsonl",
    }
    (home / "sessions" / "demo-abc123" / "s1.jsonl").write_text(
        '{"type":"message","id":"after"}\n', encoding="utf-8"
    )
    restored = await restore_backup(destination, tmp_path / "restored")
    assert (
        (restored / "sessions/demo-abc123/s1.jsonl")
        .read_text(encoding="utf-8")
        .startswith('{"type":"message","id":"a"}')
    )
    assert (restored / "gateway/sessions.jsonl").is_file()


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


async def test_restore_does_not_replace_existing_directory(tmp_path):
    home = tmp_path / "live"
    _write_tree(home)
    backup = await create_backup(home, tmp_path / "backup")
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("existing state")
    with pytest.raises(FileExistsError):
        await restore_backup(backup, target)
    assert (target / "keep").read_text() == "existing state"
