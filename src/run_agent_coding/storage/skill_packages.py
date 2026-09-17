"""Freeze Skill directories as verified cache trees. Experience assets stay Markdown."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from run_agent_coding.skills import Skill, parse_skill
from run_agent_coding.storage.canonical import canonical_json
from run_agent_coding.storage.settle import settle


class SkillPackageError(ValueError):
    pass


class ArtifactCorrupt(RuntimeError):
    """A pinned Skill cache file is missing or was modified."""


class SkillPackageStore:
    """Packages are immutable copies; the live Skill directory remains the source."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache.resolve()
        self._lock = asyncio.Lock()

    async def freeze(self, skill: Skill) -> Skill:
        async with self._lock:
            (digest, files), cancelled = await settle(asyncio.to_thread(self._capture, skill))
            if cancelled:
                raise asyncio.CancelledError
            return await self._restore(skill.name, digest, files, source_path=skill.path)

    def _capture(self, skill: Skill) -> tuple[str, list[dict[str, Any]]]:
        root = skill.path.parent.resolve(strict=True)
        rows: list[dict[str, Any]] = []
        captured: dict[Path, tuple[int, int, bytes]] = {}
        total = 0
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
                captured[path] = (after.st_size, after.st_mtime_ns, content)
                rows.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                        "executable": bool(before.st_mode & 0o111),
                    }
                )
        if not any(row["path"] == "SKILL.md" for row in rows):
            raise SkillPackageError("Skill package has no SKILL.md")
        for path, expected in captured.items():
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != expected[:2]:
                raise SkillPackageError(f"Skill changed while being captured: {path}")
        digest = hashlib.sha256(canonical_json({"files": rows}).encode("utf-8")).hexdigest()
        destination = self.cache / digest
        destination.mkdir(parents=True, exist_ok=True)
        by_path = {
            path.relative_to(root).as_posix(): content for path, (*_, content) in captured.items()
        }
        for row in rows:
            relative = PurePosixPath(row["path"])
            path = destination.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = by_path[row["path"]]
            if path.exists():
                if path.read_bytes() != content:
                    raise ArtifactCorrupt(f"Pinned Skill file was modified: {path}")
                continue
            descriptor, temporary = tempfile.mkstemp(prefix=".pin-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                os.replace(temporary, path)
                path.chmod(0o555 if row["executable"] else 0o444)
            finally:
                leftover = Path(temporary)
                if leftover.exists():
                    leftover.unlink()
        (self.cache / f"{digest}.json").write_text(
            canonical_json({"files": rows}) + "\n", encoding="utf-8"
        )
        return digest, rows

    async def restore(self, name: str, digest: str, *, source_path: Path | None = None) -> Skill:
        manifest_path = self.cache / f"{digest}.json"
        if not manifest_path.is_file():
            raise ArtifactCorrupt("Skill package manifest is missing")
        files = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
        async with self._lock:
            return await self._restore(name, digest, files, source_path=source_path)

    async def _restore(
        self,
        name: str,
        digest: str,
        files: list[dict[str, Any]],
        *,
        source_path: Path | None,
    ) -> Skill:
        def materialize() -> Skill:
            destination = self.cache / digest
            if not destination.resolve().is_relative_to(self.cache) or destination.is_symlink():
                raise ArtifactCorrupt("Skill cache leaves its root")
            for row in files:
                relative = PurePosixPath(row["path"])
                if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"]:
                    raise ArtifactCorrupt("Invalid Skill package member")
                path = destination.joinpath(*relative.parts)
                if not path.resolve().is_relative_to(destination) or path.is_symlink():
                    raise ArtifactCorrupt("Skill cache member leaves its package")
                content = path.read_bytes()
                digest_ok = hashlib.sha256(content).hexdigest() == row["sha256"]
                if not digest_ok or len(content) != row["size"]:
                    raise ArtifactCorrupt(f"Pinned Skill file was modified: {path}")
            skill = parse_skill(
                name,
                destination / "SKILL.md",
                (destination / "SKILL.md").read_text(encoding="utf-8"),
            )
            return replace(skill, package_digest=digest, source_path=source_path)

        result, cancelled = await settle(asyncio.to_thread(materialize))
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def verify(self, skills: tuple[Skill, ...]) -> None:
        for skill in skills:
            if skill.package_digest is None:
                raise SkillPackageError("Model-visible Skill has no fixed package")
            await self.restore(skill.name, skill.package_digest, source_path=skill.source_path)
