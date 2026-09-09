"""Backups include committed WAL state and the exact referenced artifact set."""

import asyncio
import json
import threading

import pytest

from run_agent_coding.host.contracts import ExtensionToken, HeadChange
from run_agent_coding.storage.artifacts import ArtifactCorrupt, ArtifactStore
from run_agent_coding.storage.backup import create_backup, restore_backup, verify_backup
from run_agent_coding.storage.resources import NamespaceResources
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import activate_extension
from run_agent_core.messages import UserMessage
from run_agent_core.session.contracts import StaleRunToken
from run_agent_core.session.entries import MessageEntry


async def setup(database, root):
    sessions = SqliteSessionRepository(database)
    await sessions.create_session(cwd=root, principal_id="local", model="test", session_id="s")
    token = await sessions.claim("s", owner_id="host", run_id="run")
    await sessions.append_entries(
        [MessageEntry(id="a", message=UserMessage(content="persist in WAL"))],
        token=token,
        expected_head=None,
    )
    ext = ExtensionToken("s", "experience", "host", 1)
    await activate_extension(database, ext)
    artifacts = ArtifactStore(root / "artifacts")
    ref = await artifacts.put(b"verified script")
    versions = NamespaceResources(database, ext, "local/project", artifacts)
    asset = await versions.put_immutable("skill", "Instructions", artifacts=[ref])
    await versions.advance_head(HeadChange("skill", None, asset.version, "manual", {}))
    return sessions, token, artifacts, ref, asset


async def test_live_wal_backup_restores_history_resources_and_fences(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "live" / "state.sqlite3") as database:
        sessions, token, artifacts, ref, asset = await setup(database, tmp_path / "live")
        assert database.path.with_name("state.sqlite3-wal").exists()
        destination = await create_backup(database, artifacts, tmp_path / "backup")
        manifest = await verify_backup(destination)
        assert manifest["watermarks"] == {"s": 1}
        await sessions.append_entries(
            [MessageEntry(id="b", parent_id="a", message=UserMessage(content="after snapshot"))],
            token=token,
            expected_head="a",
        )
        await restore_backup(destination, tmp_path / "restored")
    async with await SqliteDatabase.open(tmp_path / "restored" / "state.sqlite3") as database:
        sessions = SqliteSessionRepository(database)
        assert [item.id for item in (await sessions.read_entries("s")).entries] == ["a"]
        with pytest.raises(StaleRunToken):
            await sessions.renew(token)
        guard = await database.run(
            lambda connection: connection.execute(
                "SELECT value_json FROM host_metadata WHERE key='restore_guard'"
            ).fetchone()[0]
        )
        assert json.loads(guard)["requires_reconciliation"] is True
        await sessions.claim("s", owner_id="new-host", run_id="new-run")
        ext = ExtensionToken("s", "experience", "new-host", 1)
        await activate_extension(database, ext)
        restored_artifacts = ArtifactStore(tmp_path / "restored" / "artifacts")
        resources = NamespaceResources(database, ext, "local/project", restored_artifacts)
        assert (await resources.resolve("skill", asset.version)).content == "Instructions"
        assert await restored_artifacts.read(ref) == b"verified script"


async def test_missing_artifact_fails_backup_without_publishing_partial_package(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        _, _, artifacts, ref, _ = await setup(database, tmp_path)
        artifacts.path(ref.digest).unlink()
        with pytest.raises(ArtifactCorrupt):
            await create_backup(database, artifacts, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
        assert not list(tmp_path.glob(".run-backup-*"))


async def test_tampered_backup_fails_before_creating_restore_target(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        _, _, artifacts, ref, _ = await setup(database, tmp_path)
        backup = await create_backup(database, artifacts, tmp_path / "backup")
        ArtifactStore(backup / "artifacts").path(ref.digest).write_bytes(b"tampered")
        with pytest.raises(ArtifactCorrupt):
            await restore_backup(backup, tmp_path / "restored")
        assert not (tmp_path / "restored").exists()


async def test_restore_does_not_replace_existing_directory(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        _, _, artifacts, _, _ = await setup(database, tmp_path)
        backup = await create_backup(database, artifacts, tmp_path / "backup")
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("existing state")
    with pytest.raises(FileExistsError):
        await restore_backup(backup, target)
    assert (target / "keep").read_text() == "existing state"


async def test_cancelling_queued_backup_drains_snapshot_before_removing_staging(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        _, _, artifacts, _, _ = await setup(database, tmp_path)
        started, release = threading.Event(), threading.Event()

        def barrier(connection):
            started.set()
            assert release.wait(5)

        occupying = asyncio.create_task(database.run(barrier))
        assert await asyncio.to_thread(started.wait, 5)
        backup = asyncio.create_task(create_backup(database, artifacts, tmp_path / "backup"))
        await asyncio.sleep(0)
        backup.cancel()
        await asyncio.sleep(0)
        try:
            assert not backup.done()
        finally:
            release.set()
        await occupying
        with pytest.raises(asyncio.CancelledError):
            await backup
        assert not (tmp_path / "backup").exists()
        assert not list(tmp_path.glob(".run-backup-*"))
