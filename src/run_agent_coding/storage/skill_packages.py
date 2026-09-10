"""Freeze complete Skill directories as verified artifacts and executable paths."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from time import time
from typing import Any

from run_agent_coding.host.contracts import ArtifactRef
from run_agent_coding.skills import Skill, parse_skill
from run_agent_coding.storage.artifacts import ArtifactCorrupt, ArtifactStore
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.settle import settle
from run_agent_coding.storage.sqlite import SqliteDatabase


class SkillPackageError(ValueError):
    pass


class SkillPackageStore:
    """Packages are immutable; cache directories can be rebuilt from a backup.

    Read-only cache permissions prevent ordinary accidental edits. This is not
    an OS sandbox: trusted Python extensions and unrestricted shells can change
    permissions. Every model request verifies its pinned packages before use.
    """

    def __init__(self, database: SqliteDatabase, artifacts: ArtifactStore, cache: Path) -> None:
        self.database, self.artifacts, self.cache = database, artifacts, cache.resolve()
        self._lock = asyncio.Lock()

    async def freeze(self, skill: Skill) -> Skill:
        async with self._lock:
            (manifest, refs), cancelled = await settle(asyncio.to_thread(self._capture, skill))
            if cancelled:
                raise asyncio.CancelledError

            def register(connection: sqlite3.Connection) -> None:
                for ref in refs:
                    connection.execute(
                        "INSERT OR IGNORE INTO artifacts VALUES (?,?,?)",
                        (ref.digest, ref.size, time()),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO artifact_refs VALUES ('skill_package',?,?)",
                        (manifest.digest, ref.digest),
                    )

            await self.database.run(register, write=True)
            return await self._restore(skill.name, manifest, source_path=skill.path)

    def _capture(self, skill: Skill) -> tuple[ArtifactRef, tuple[ArtifactRef, ...]]:
        root = skill.path.parent.resolve(strict=True)
        rows: list[dict[str, Any]] = []
        refs: list[ArtifactRef] = []
        captured: dict[Path, tuple[int, int]] = {}
        total = 0
        # Symlinked Skill roots are allowed, but package contents cannot reach
        # mutable files outside that root or recurse through directory links.
        for folder, directories, files in os.walk(root, followlinks=False):
            for directory in directories:
                candidate = Path(folder) / directory
                if candidate.is_symlink() or candidate.is_junction():
                    raise SkillPackageError(f"Skill contains a directory link: {candidate}")
            directories[:] = sorted(d for d in directories if d not in {".git", "__pycache__"})
            for name in sorted(files):
                path = Path(folder) / name
                if path.is_symlink():
                    raise SkillPackageError(f"Skill contains a file link: {path}")
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise SkillPackageError(f"Skill contains a non-regular file: {path}")
                total += before.st_size
                if len(rows) >= 1024 or total > 16 * 1024 * 1024:
                    raise SkillPackageError("Skill package exceeds 1024 files or 16 MiB")
                content = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise SkillPackageError(f"Skill changed while being captured: {path}")
                captured[path] = (after.st_size, after.st_mtime_ns)
                ref = self.artifacts.put_sync(content)
                refs.append(ref)
                rows.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "artifact": asdict(ref),
                        "executable": bool(before.st_mode & 0o111),
                    }
                )
        if not any(row["path"] == "SKILL.md" for row in rows):
            raise SkillPackageError("Skill package has no SKILL.md")
        # Detect edits to an earlier member while later members were captured.
        for path, expected in captured.items():
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != expected:
                raise SkillPackageError(f"Skill changed while being captured: {path}")
        manifest = self.artifacts.put_sync(canonical_json({"files": rows}).encode("utf-8"))
        return manifest, (*refs, manifest)

    async def restore(self, name: str, digest: str, *, source_path: Path | None = None) -> Skill:
        def ref(connection: sqlite3.Connection) -> ArtifactRef:
            row = connection.execute(
                "SELECT size FROM artifacts a JOIN artifact_refs r ON a.digest=r.digest "
                "WHERE a.digest=? AND r.owner_kind='skill_package' AND r.owner_key=?",
                (digest, digest),
            ).fetchone()
            if row is None:
                raise ArtifactCorrupt("Skill package manifest is missing")
            return ArtifactRef(digest, row[0])

        manifest = await self.database.run(ref)
        async with self._lock:
            return await self._restore(name, manifest, source_path=source_path)

    async def _restore(
        self, name: str, manifest: ArtifactRef, *, source_path: Path | None
    ) -> Skill:
        def materialize() -> Skill:
            body = json.loads(self.artifacts.read_sync(manifest))
            destination = self.cache / manifest.digest
            if not destination.resolve().is_relative_to(self.cache) or destination.is_symlink():
                raise ArtifactCorrupt("Skill cache leaves its root")
            destination.mkdir(parents=True, exist_ok=True)
            for row in body["files"]:
                relative = PurePosixPath(row["path"])
                if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"]:
                    raise ArtifactCorrupt("Invalid Skill package member")
                path = destination.joinpath(*relative.parts)
                if not path.resolve().is_relative_to(destination) or path.is_symlink():
                    raise ArtifactCorrupt("Skill cache member leaves its package")
                ref = ArtifactRef(**row["artifact"])
                content = self.artifacts.read_sync(ref)
                if path.exists():
                    if path.read_bytes() != content:
                        raise ArtifactCorrupt(f"Pinned Skill file was modified: {path}")
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix=".pin-", dir=path.parent)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                    os.replace(temporary, path)
                    path.chmod(0o555 if row["executable"] else 0o444)
                finally:
                    if Path(temporary).exists():
                        Path(temporary).unlink()
            skill = parse_skill(
                name,
                destination / "SKILL.md",
                (destination / "SKILL.md").read_text(encoding="utf-8"),
            )
            return replace(skill, package_digest=manifest.digest, source_path=source_path)

        result, cancelled = await settle(asyncio.to_thread(materialize))
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def verify(self, skills: tuple[Skill, ...]) -> None:
        for skill in skills:
            if skill.package_digest is None:
                raise SkillPackageError("Model-visible Skill has no fixed package")
            await self.restore(skill.name, skill.package_digest, source_path=skill.source_path)
