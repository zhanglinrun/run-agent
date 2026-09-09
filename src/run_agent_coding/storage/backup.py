"""Consistent SQLite snapshots and verified immutable artifacts in one package."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from run_agent_coding.host.contracts import ArtifactRef
from run_agent_coding.storage.artifacts import ArtifactCorrupt, ArtifactStore
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.sqlite import APPLICATION_ID, SCHEMA_VERSION, SqliteDatabase

BACKUP_SCHEMA = "run.backup.v1"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ValueError("Backup does not contain a Run Agent database")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Unsupported backup database schema")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Backup database failed integrity verification")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Backup database contains broken references")


def _references(connection: sqlite3.Connection) -> list[ArtifactRef]:
    rows = connection.execute(
        """SELECT DISTINCT a.digest, a.size FROM artifacts a JOIN artifact_refs r
           ON a.digest=r.digest ORDER BY a.digest"""
    ).fetchall()
    return [ArtifactRef(row[0], row[1]) for row in rows]


async def create_backup(
    database: SqliteDatabase, artifacts: ArtifactStore, destination: str | Path
) -> Path:
    """Publish only complete packages; never copy a live database file directly.

    Artifact collection is append-only in this version. Before adding a garbage
    collector, it must coordinate retention with backup creation.
    """
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".run-backup-", dir=destination.parent))
    try:

        def snapshot(source: sqlite3.Connection) -> None:
            target = sqlite3.connect(staging / "state.sqlite3")
            try:
                source.backup(target)
                _verify_database(target)
            finally:
                target.close()

        snapshot_work = asyncio.create_task(database.run(snapshot))
        try:
            await asyncio.shield(snapshot_work)
        except asyncio.CancelledError:
            await snapshot_work
            raise

        def collect() -> None:
            path = staging / "state.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                refs = _references(connection)
                watermarks = dict(connection.execute("SELECT session_id, last_seq FROM sessions"))
            copied = ArtifactStore(staging / "artifacts")
            for ref in refs:
                actual = copied.put_sync(artifacts.read_sync(ref))
                if actual != ref:
                    raise ArtifactCorrupt("Backup artifact differs from its database reference")
            manifest = {
                "schema": BACKUP_SCHEMA,
                "database_schema": SCHEMA_VERSION,
                "application_version": version("run-agent-harness"),
                "created_at": time(),
                "database_sha256": _digest(path),
                "watermarks": watermarks,
                "artifacts": [{"digest": ref.digest, "size": ref.size} for ref in refs],
            }
            with (staging / "manifest.json").open("w", encoding="utf-8") as stream:
                stream.write(canonical_json(manifest) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(staging, destination)

        # Shield admitted filesystem work and drain on cancellation before cleanup.
        work = asyncio.create_task(asyncio.to_thread(collect))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def verify_backup(source: str | Path) -> dict[str, Any]:
    return await asyncio.to_thread(_verify_backup, Path(source).resolve())


def _verify_backup(source: Path) -> dict[str, Any]:
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError("Unsupported backup manifest")
    database = source / "state.sqlite3"
    if _digest(database) != manifest.get("database_sha256"):
        raise ValueError("Backup database hash mismatch")
    # mode=ro prevents an empty replacement database being created during verification.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        _verify_database(connection)
        refs = _references(connection)
    declared = [ArtifactRef(**item) for item in manifest.get("artifacts", [])]
    if refs != declared:
        raise ValueError("Backup manifest does not match the referenced artifacts")
    artifacts = ArtifactStore(source / "artifacts")
    for ref in refs:
        artifacts.read_sync(ref)
    return manifest


async def restore_backup(source: str | Path, destination: str | Path) -> Path:
    """Restore to a new directory and fence old workers; never switch a live host."""
    source, destination = Path(source).resolve(), Path(destination).resolve()

    def restore() -> Path:
        manifest = _verify_backup(source)
        if destination.exists():
            raise FileExistsError(f"Restore destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".run-restore-", dir=destination.parent))
        try:
            shutil.copyfile(source / "state.sqlite3", staging / "state.sqlite3")
            original, copied = (
                ArtifactStore(source / "artifacts"),
                ArtifactStore(staging / "artifacts"),
            )
            for entry in manifest["artifacts"]:
                copied.put_sync(original.read_sync(ArtifactRef(**entry)))
            with closing(sqlite3.connect(staging / "state.sqlite3")) as connection, connection:
                connection.execute("PRAGMA foreign_keys=ON")
                # External effects do not rewind with a database snapshot.
                connection.execute("UPDATE sessions SET generation=generation+1, owner_active=0")
                connection.execute("UPDATE extension_owners SET active=0")
                connection.execute(
                    "INSERT OR REPLACE INTO host_metadata VALUES ('restore_guard', ?)",
                    (
                        canonical_json(
                            {
                                "restore_id": uuid4().hex,
                                "created_at": time(),
                                "source_database_sha256": manifest["database_sha256"],
                                "requires_reconciliation": True,
                            }
                        ),
                    ),
                )
                _verify_database(connection)
            os.rename(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination

    work = asyncio.create_task(asyncio.to_thread(restore))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await work
        raise
