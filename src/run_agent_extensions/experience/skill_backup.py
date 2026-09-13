"""Whole-library snapshots of the skills directories, and rollback to one.

A port of hermes-agent's ``agent/curator_backup.py``. Before any mutating curator pass
(and on ``/curator backup``) every scope's skills directory is tarred into
``<state_dir>/.curator_backups/<utc-id>/skills-<scope>.tar.gz`` next to a
``manifest.json`` (reason, time, sizes, counts). Only the newest ``keep`` snapshots
survive. Rollback picks a snapshot, first takes a safety snapshot of the current trees
(protected from pruning) so the rollback itself is undoable, moves the live trees aside
into a staging directory, extracts, and moves them back on failure.

What a snapshot includes: every skill directory (SKILL.md and support files), the
``.usage.json`` sidecar, ``.archive/``, the ledger and its blobs, so restoring a
snapshot also restores the lifecycle state and the audit trail that go with it. The
backups directory itself is never included.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKUPS_DIR = ".curator_backups"
DEFAULT_KEEP = 5
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(-\d{2})?$")
_STAGING_PREFIX = ".rollback-staging-"
# Never rolled up into a snapshot: the backups themselves.
_EXCLUDE_TOP_LEVEL = {BACKUPS_DIR}


@dataclass(frozen=True, slots=True)
class BackupInfo:
    backup_id: str
    path: Path
    reason: str
    created_at: str
    skill_files: int
    archive_bytes: int
    scopes: tuple[str, ...]


class SkillBackups:
    """Snapshots of the skills roots under one backups directory."""

    def __init__(
        self,
        roots: Mapping[str, Path],
        backups_dir: Path,
        *,
        keep: int = DEFAULT_KEEP,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.roots = dict(roots)
        self.backups_dir = backups_dir
        self.keep = max(1, keep)
        self.enabled = enabled
        self.clock = clock

    # -- snapshot ---------------------------------------------------------------------

    def snapshot(self, reason: str = "manual", *, protect: set[str] | None = None) -> Path | None:
        """Snapshot every existing root; ``None`` when disabled, empty or failed.

        A failure is logged and swallowed: the curator must never abort a pass because a
        backup could not be written, and ``protect`` keeps named snapshots out of the
        prune that follows.
        """
        if not self.enabled:
            return None
        present = {scope: root for scope, root in self.roots.items() if root.is_dir()}
        if not present:
            return None
        try:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("backup dir create failed", exc_info=True)
            return None
        base = self._utc_id()
        snap_id = base
        counter = 1
        while (self.backups_dir / snap_id).exists():
            snap_id = f"{base}-{counter:02d}"
            counter += 1
        dest = self.backups_dir / snap_id
        try:
            dest.mkdir(parents=True, exist_ok=False)
            total_files = 0
            total_bytes = 0
            for scope, root in present.items():
                archive = dest / f"skills-{scope}.tar.gz"
                with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
                    for entry in sorted(root.iterdir()):
                        if entry.name in _EXCLUDE_TOP_LEVEL:
                            continue
                        tar.add(str(entry), arcname=entry.name, recursive=True)
                total_files += sum(1 for p in root.rglob("SKILL.md") if p.is_file())
                total_bytes += archive.stat().st_size
            manifest = {
                "id": snap_id,
                "reason": reason,
                "created_at": datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
                "scopes": sorted(present),
                "skill_files": total_files,
                "archive_bytes": total_bytes,
            }
            (dest / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except (OSError, tarfile.TarError):
            logger.debug("curator snapshot failed", exc_info=True)
            shutil.rmtree(dest, ignore_errors=True)
            return None
        self._prune(protect=protect or set())
        return dest

    # -- listing ----------------------------------------------------------------------

    def list_backups(self) -> list[BackupInfo]:
        """Restorable snapshots, newest first; staging leftovers are not listed."""
        if not self.backups_dir.is_dir():
            return []
        found: list[BackupInfo] = []
        for child in sorted(self.backups_dir.iterdir(), reverse=True):
            if not child.is_dir() or not _ID_RE.match(child.name):
                continue
            archives = list(child.glob("skills-*.tar.gz"))
            if not archives:
                continue
            manifest = self._read_manifest(child)
            found.append(
                BackupInfo(
                    backup_id=child.name,
                    path=child,
                    reason=str(manifest.get("reason") or "?"),
                    created_at=str(manifest.get("created_at") or ""),
                    skill_files=int(manifest.get("skill_files") or 0),
                    archive_bytes=int(
                        manifest.get("archive_bytes") or sum(a.stat().st_size for a in archives)
                    ),
                    scopes=tuple(str(s) for s in (manifest.get("scopes") or []))
                    or tuple(sorted(a.name[len("skills-") : -len(".tar.gz")] for a in archives)),
                )
            )
        return found

    def summarize(self) -> str:
        rows = self.list_backups()
        if not rows:
            return "No curator snapshots yet."
        lines = [f"{'id':<24}  {'reason':<40}  {'skills':>6}  {'size':>8}"]
        lines.append("─" * len(lines[0]))
        for row in rows:
            lines.append(
                f"{row.backup_id:<24}  {row.reason[:40]:<40}  {row.skill_files:>6}  "
                f"{_format_bytes(row.archive_bytes):>8}"
            )
        return "\n".join(lines)

    # -- rollback ---------------------------------------------------------------------

    def rollback(self, backup_id: str | None = None) -> tuple[bool, str, Path | None]:
        """Restore every root from a snapshot (the newest when ``backup_id`` is None).

        Fail-closed: the safety snapshot must succeed before anything moves, and a failed
        extract puts the staged trees back.
        """
        target = self._resolve(backup_id)
        if target is None:
            return (
                False,
                "no matching backup found"
                + (f" for id {backup_id!r}" if backup_id else "")
                + " (use /curator backups to see available snapshots)",
                None,
            )
        archives = {
            a.name[len("skills-") : -len(".tar.gz")]: a for a in target.glob("skills-*.tar.gz")
        }
        if not archives:
            return False, f"snapshot {target.name} has no skills archive — corrupted?", None
        safety = self.snapshot(reason=f"pre-rollback to {target.name}", protect={target.name})
        if safety is None:
            return (
                False,
                "pre-rollback safety snapshot failed; backups may be disabled or unavailable, "
                "and current skills were not changed",
                None,
            )
        staged_root = self.backups_dir / f"{_STAGING_PREFIX}{self._utc_id()}"
        try:
            staged_root.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            return False, f"failed to create staging dir: {exc}", None
        moved: list[tuple[Path, Path]] = []
        try:
            for scope, root in self.roots.items():
                if scope not in archives or not root.is_dir():
                    continue
                stage = staged_root / scope
                stage.mkdir(parents=True, exist_ok=True)
                for entry in list(root.iterdir()):
                    if entry.name in _EXCLUDE_TOP_LEVEL:
                        continue
                    dest = stage / entry.name
                    shutil.move(str(entry), str(dest))
                    moved.append((entry, dest))
        except OSError as exc:
            self._unstage(moved)
            shutil.rmtree(staged_root, ignore_errors=True)
            return False, f"failed to stage current skills: {exc}", None
        try:
            for scope, archive in archives.items():
                restore_root = self.roots.get(scope)
                if restore_root is None:
                    continue
                restore_root.mkdir(parents=True, exist_ok=True)
                with tarfile.open(archive, "r:gz") as tar:
                    for member in tar.getmembers():
                        if member.name.startswith("/") or ".." in Path(member.name).parts:
                            raise tarfile.TarError(
                                f"refusing to extract unsafe path {member.name!r}"
                            )
                    tar.extractall(str(restore_root), filter="data")
        except (OSError, tarfile.TarError) as exc:
            staged_names = {orig for orig, _ in moved}
            for _scope, root in self.roots.items():
                if not root.is_dir():
                    continue
                for entry in list(root.iterdir()):
                    if entry.name in _EXCLUDE_TOP_LEVEL or entry in staged_names:
                        continue
                    _remove(entry)
            failed = self._unstage(moved)
            if failed:
                return (
                    False,
                    f"snapshot extract failed: {exc} - could not restore "
                    f"{', '.join(sorted(failed))}; staged copies kept at {staged_root}",
                    None,
                )
            shutil.rmtree(staged_root, ignore_errors=True)
            return False, f"snapshot extract failed (state restored): {exc}", None
        shutil.rmtree(staged_root, ignore_errors=True)
        logger.info("curator rollback: restored from %s", target.name)
        return True, f"restored from snapshot {target.name}", target

    # -- internals --------------------------------------------------------------------

    def _utc_id(self) -> str:
        return datetime.fromtimestamp(self.clock(), tz=UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

    def _resolve(self, backup_id: str | None) -> Path | None:
        if not self.backups_dir.is_dir():
            return None
        if backup_id:
            target = self.backups_dir / backup_id
            if target.is_dir() and _ID_RE.match(backup_id) and any(target.glob("skills-*.tar.gz")):
                return target
            return None
        for info in self.list_backups():
            return info.path
        return None

    def _prune(self, *, protect: set[str]) -> list[str]:
        if not self.backups_dir.is_dir():
            return []
        regular: list[Path] = []
        stale_staging: list[Path] = []
        for child in self.backups_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith(_STAGING_PREFIX):
                stale_staging.append(child)
            elif _ID_RE.match(child.name):
                regular.append(child)
        regular.sort(key=lambda p: p.name, reverse=True)
        deleted: list[str] = []
        for path in regular[self.keep :]:
            if path.name in protect:
                continue
            try:
                shutil.rmtree(path)
                deleted.append(path.name)
            except OSError:
                logger.debug("failed to prune %s", path, exc_info=True)
        for path in stale_staging:
            shutil.rmtree(path, ignore_errors=True)
        return deleted

    @staticmethod
    def _read_manifest(snap_dir: Path) -> dict[str, Any]:
        manifest = snap_dir / "manifest.json"
        if not manifest.is_file():
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _unstage(moved: list[tuple[Path, Path]]) -> list[str]:
        """Move staged entries back; the names that could not be restored."""
        failed: list[str] = []
        for original, staged in moved:
            try:
                _remove(original)
                shutil.move(str(staged), str(original))
            except OSError:
                failed.append(original.name)
        return failed


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _format_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


__all__ = ["BACKUPS_DIR", "DEFAULT_KEEP", "BackupInfo", "SkillBackups"]
