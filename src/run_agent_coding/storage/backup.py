"""Copy session trees and gateway JSONL files into a verified backup directory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from time import time
from typing import Any

from run_agent_coding.storage.canonical import canonical_json

BACKUP_SCHEMA = "run.backup.v2"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


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
    files: list[Path] = []
    sessions = home / "sessions"
    if sessions.exists():
        for path in sessions.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                files.append(path)
    gateway = home / "gateway"
    if gateway.exists():
        for name in ("sessions.jsonl", "deliveries.jsonl"):
            candidate = gateway / name
            if candidate.is_file():
                files.append(candidate)
    collected = sorted(files)
    for path in collected:
        if path.name == "index.jsonl":
            _assert_index_complete(path)
    return collected


async def create_backup(home: str | Path, destination: str | Path) -> Path:
    """Publish a complete copy of sessions/ and gateway JSONL files."""
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
                raise FileNotFoundError(f"No session or gateway JSONL files under {home}")
            entries: list[dict[str, Any]] = []
            for path in files:
                relative = path.relative_to(home).as_posix()
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                entries.append(
                    {"path": relative, "sha256": _digest(target), "size": target.stat().st_size}
                )
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
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError("Unsupported backup manifest")
    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        raise ValueError("Backup manifest lists no files")
    for item in declared:
        path = source / str(item["path"])
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
