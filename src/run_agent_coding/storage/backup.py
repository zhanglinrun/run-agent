"""Copy session trees into a verified backup directory.

The global session catalog (``<sessions>/index.jsonl``) is a derived cache: the project
indexes inside the same tree are the source of truth. Publishing a backup and restoring
one both rebuild that catalog from those project indexes, so a verified package never
carries a stale cache and a restore never has to promise a cross-file transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import suppress
from pathlib import Path
from time import time
from typing import Any

from run_agent_coding.storage.canonical import canonical_json
from run_agent_core.session.storage import _fsync_directory

BACKUP_SCHEMA = "run.backup.v3"
SUPPORTED_BACKUP_SCHEMAS = frozenset({"run.backup.v2", BACKUP_SCHEMA})
CATALOG_NAME = "index.jsonl"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rebuild_catalog(sessions_root: str | Path) -> Path:
    """Recompute ``<sessions_root>/index.jsonl`` from the project indexes in that tree.

    Rows still readable in the existing catalog are kept as a fallback, because a backed-up
    home may not contain the project index of every session it knows about; a project index
    row always wins on conflict (last write wins by ``updated_at``). Relative session paths
    are re-homed to the catalog's own directory so the rebuilt catalog stays self-consistent
    with the tree it now describes.

    Cost: one scan of the session tree that is already being copied, verified, or restored -
    never a scan of anything outside it.
    """
    root = Path(sessions_root)
    catalog = root / CATALOG_NAME
    records: dict[str, dict[str, Any]] = {}
    if catalog.is_file():
        for payload in _index_rows(catalog):
            record_id = _record_id(payload)
            if record_id is not None:
                records[record_id] = payload
    for index_path in sorted(root.rglob(CATALOG_NAME)):
        if index_path == catalog:
            continue
        for payload in _index_rows(index_path):
            record_id = _record_id(payload)
            if record_id is None:
                continue
            candidate = _rehost_path(payload, index_path, root)
            existing = records.get(record_id)
            if existing is None or _updated_at(candidate) >= _updated_at(existing):
                records[record_id] = candidate
    root.mkdir(parents=True, exist_ok=True)
    _write_catalog(catalog, records.values())
    return catalog


def _index_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each parseable JSON object in an index, skipping torn lines."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def _record_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("id")
    return value if isinstance(value, str) and value else None


def _updated_at(payload: dict[str, Any]) -> float:
    try:
        return float(payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _rehost_path(payload: dict[str, Any], index_path: Path, root: Path) -> dict[str, Any]:
    """Rewrite a session path so it resolves relative to the catalog, not the project."""
    raw = payload.get("path")
    if not raw:
        return payload
    candidate = Path(str(raw))
    if candidate.is_absolute():
        return payload
    try:
        relative = (index_path.parent / candidate).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return payload
    return {**payload, "path": relative.as_posix()}


def _write_catalog(catalog: Path, records: Iterable[dict[str, Any]]) -> None:
    """Replace the catalog atomically, keeping the old file on any failure."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{catalog.name}.", suffix=".tmp", dir=catalog.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, catalog)
        _fsync_directory(catalog.parent)
    except BaseException:
        with suppress(OSError):
            temporary_path.unlink()
        raise


def _assert_index_complete(index_path: Path) -> None:
    for line in index_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid session index {index_path}") from exc
        if not isinstance(payload, dict):
            continue
        relative = payload.get("path")
        if not relative:
            continue
        candidate = Path(str(relative))
        if not candidate.is_absolute():
            candidate = index_path.parent / candidate
        if not candidate.is_file():
            raise FileNotFoundError(f"Session file missing: {candidate}")


def _collect_files(home: Path) -> list[Path]:
    sessions = home / "sessions"
    files = (
        [path for path in sessions.rglob("*") if path.is_file() and not path.name.startswith(".")]
        if sessions.exists()
        else []
    )
    collected = sorted(files)
    for path in collected:
        if path.name == "index.jsonl":
            _assert_index_complete(path)
    return collected


async def create_backup(home: str | Path, destination: str | Path) -> Path:
    """Publish a complete copy of the session tree."""
    home = Path(home).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".run-backup-", dir=destination.parent))
    try:

        def snapshot() -> None:
            files = _collect_files(home)
            if not files:
                raise FileNotFoundError(f"No session files under {home}")
            for path in files:
                target = staging / path.relative_to(home)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            # The published package carries a catalog rebuilt from its own project indexes.
            sessions = staging / "sessions"
            if sessions.is_dir():
                rebuild_catalog(sessions)
            entries = [
                {
                    "path": path.relative_to(staging).as_posix(),
                    "sha256": _digest(path),
                    "size": path.stat().st_size,
                }
                for path in _collect_files(staging)
            ]
            manifest = {
                "schema": BACKUP_SCHEMA,
                "created_at": time(),
                "files": entries,
            }
            with (staging / "manifest.json").open("w", encoding="utf-8") as stream:
                stream.write(canonical_json(manifest) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(staging, destination)

        work = asyncio.create_task(asyncio.to_thread(snapshot))
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
    if not isinstance(manifest, dict) or manifest.get("schema") not in SUPPORTED_BACKUP_SCHEMAS:
        raise ValueError("Unsupported backup manifest")
    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise ValueError("Backup manifest lists no files")
    for item in declared:
        relative = Path(str(item["path"]))
        if manifest["schema"] == BACKUP_SCHEMA and (
            not relative.parts or relative.parts[0] != "sessions"
        ):
            raise ValueError("run.backup.v3 manifests may only contain session files")
        path = source / relative
        if not path.is_file():
            raise FileNotFoundError(f"Backup is missing {item['path']}")
        if _digest(path) != item.get("sha256") or path.stat().st_size != item.get("size"):
            raise ValueError(f"Backup file hash mismatch: {item['path']}")
    return manifest


async def restore_backup(source: str | Path, destination: str | Path) -> Path:
    source, destination = Path(source).resolve(), Path(destination).resolve()

    def restore() -> Path:
        manifest = _verify_backup(source)
        if destination.exists():
            raise FileExistsError(f"Restore destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".run-restore-", dir=destination.parent))
        try:
            shutil.copy2(source / "manifest.json", staging / "manifest.json")
            for item in manifest["files"]:
                relative = str(item["path"])
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
            # A restored tree gets the same treatment as a published one: the catalog is
            # rebuilt from the project indexes that travelled with it.
            sessions = staging / "sessions"
            if sessions.is_dir():
                rebuild_catalog(sessions)
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
