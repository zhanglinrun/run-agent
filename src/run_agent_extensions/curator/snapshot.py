"""Whole-library snapshots and rollback for one Skill root.

A snapshot is a ``tar.gz`` of one Skill root plus a small ``manifest.json``:

```text
<state.extension_state_dir>/curator/backups/<utc-id>/
    skills.tar.gz
    manifest.json   {id, reason, created_at, archive, archive_bytes, skill_files, root}
```

Snapshots are written *before* a mutating Curator pass, never after, so the file that
is needed to undo a pass already exists when the pass starts. ``prune_old`` keeps the
newest ``keep`` snapshots and never removes an id the caller protects (the snapshot a
rollback is about to read from, or a pre-rollback safety snapshot).

``restore`` deliberately behaves like a transaction:

1. take a pre-restore snapshot of the current tree (the rollback's own undo handle);
2. move every current top-level entry into ``<root>/.rollback-staging-<id>``;
3. validate every tar member (no absolute path, no ``..``, no symlink, hardlink or
   device member);
4. extract into the now-empty root;
5. on any failure, delete whatever the extract created and move the staged entries
   back - and when that also fails, keep the staging directory and say so in the
   message instead of claiming a clean restore;
6. on success, delete the staging directory.

The whole sequence runs inside whatever lock the caller passes (the extension passes
``SkillManager.write_scope``), which hermes has no equivalent of.

Only self-owned bookkeeping is excluded from the archive: a nested backup directory
and the Skill write lock. ``.ledger.jsonl``, the ledger's ``.blobs`` and ``.archive``
are deliberately *included*, so a rollback also restores the audit trail and the
previously archived Skills.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Collection, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_ARCHIVE = "skills.tar.gz"
SNAPSHOT_MANIFEST = "manifest.json"
BACKUPS_DIR = "backups"
STAGING_PREFIX = ".rollback-staging-"
DEFAULT_KEEP = 5
MAX_MANIFEST_BYTES = 65_536
# Bookkeeping that must not be captured: our own backup tree (a nested copy would
# recurse) and the formal Skill write lock.
EXCLUDE_NAMES = frozenset({".curator_backups", ".write.lock"})
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(-\d{2})?$")


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """One restorable snapshot."""

    id: str
    path: Path
    archive: str
    reason: str
    created_at: str
    archive_bytes: int
    skill_files: int
    root: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the manifest fields (plus the resolved directory) for display."""
        return {
            "id": self.id,
            "path": str(self.path),
            "reason": self.reason,
            "created_at": self.created_at,
            "archive": self.archive,
            "archive_bytes": self.archive_bytes,
            "skill_files": self.skill_files,
            "root": self.root,
        }


def utc_id(now: datetime | None = None) -> str:
    """Return the filesystem-safe UTC id used for a snapshot directory."""
    moment = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    text = moment.isoformat()
    if text.endswith("+00:00"):
        text = text[: -len("+00:00")]
    return text.replace(":", "-") + "Z"


def backups_dir(state_dir: Path) -> Path:
    """Return the snapshot root under a Curator state directory."""
    return state_dir / "curator" / BACKUPS_DIR


def snapshot_skills(
    root: Path,
    *,
    reason: str,
    state_dir: Path,
    keep: int = DEFAULT_KEEP,
    protect: Collection[str] = (),
    now: datetime | None = None,
) -> SnapshotRef | None:
    """Archive one Skill root; return ``None`` when no snapshot could be taken.

    A snapshot failure must never block a Curator pass, so every error is logged and
    reported as ``None``. Callers that need the guarantee (rollback) treat ``None`` as
    a hard stop instead.
    """
    if not root.is_dir():
        logger.debug("curator snapshot skipped: %s is not a directory", root)
        return None
    backups = backups_dir(state_dir)
    try:
        backups.mkdir(parents=True, exist_ok=True)
        snapshot_id = _free_id(backups, now)
        destination = backups / snapshot_id
        destination.mkdir(parents=True, exist_ok=False)
    except OSError:
        logger.warning("curator snapshot directory could not be created", exc_info=True)
        return None
    archive = destination / SNAPSHOT_ARCHIVE
    skill_files = count_skill_files(root)
    created_at = datetime.now(UTC).isoformat()
    try:
        with tarfile.open(archive, "w:gz", compresslevel=6) as handle:
            for entry in sorted(root.iterdir(), key=lambda item: item.name):
                if _excluded(entry.name):
                    continue
                handle.add(str(entry), arcname=entry.name, recursive=True)
        archive_bytes = archive.stat().st_size
        manifest: dict[str, Any] = {
            "id": snapshot_id,
            "reason": reason,
            "created_at": created_at,
            "archive": SNAPSHOT_ARCHIVE,
            "archive_bytes": archive_bytes,
            "skill_files": skill_files,
            "root": str(root),
        }
        _atomic_write_json(destination / SNAPSHOT_MANIFEST, manifest)
    except (OSError, tarfile.TarError):
        logger.warning("curator snapshot failed for %s", root, exc_info=True)
        shutil.rmtree(destination, ignore_errors=True)
        return None
    prune_old(state_dir, keep=keep, protect={*protect, snapshot_id})
    return SnapshotRef(
        id=snapshot_id,
        path=destination,
        archive=SNAPSHOT_ARCHIVE,
        reason=reason,
        created_at=created_at,
        archive_bytes=archive_bytes,
        skill_files=skill_files,
        root=str(root),
    )


def prune_old(
    state_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    protect: Collection[str] = (),
) -> tuple[str, ...]:
    """Delete snapshots beyond the newest ``keep``; never delete a protected id."""
    backups = backups_dir(state_dir)
    if not backups.is_dir():
        return ()
    entries = sorted(
        (child for child in backups.iterdir() if child.is_dir() and _ID_RE.match(child.name)),
        key=lambda item: item.name,
        reverse=True,
    )
    protected = set(protect)
    deleted: list[str] = []
    for path in entries[max(1, keep) :]:
        if path.name in protected:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            logger.warning("curator snapshot prune failed for %s", path, exc_info=True)
            continue
        deleted.append(path.name)
    return tuple(deleted)


def list_snapshots(state_dir: Path) -> tuple[SnapshotRef, ...]:
    """Return every restorable snapshot, newest first."""
    backups = backups_dir(state_dir)
    if not backups.is_dir():
        return ()
    found: list[SnapshotRef] = []
    for child in sorted(backups.iterdir(), key=lambda item: item.name, reverse=True):
        if not child.is_dir() or not _ID_RE.match(child.name):
            continue
        if not (child / SNAPSHOT_ARCHIVE).is_file():
            continue
        found.append(_read_snapshot(child))
    return tuple(found)


def resolve_snapshot(state_dir: Path, snapshot_id: str | None = None) -> SnapshotRef | None:
    """Return one snapshot by id, or the newest one when no id is given."""
    snapshots = list_snapshots(state_dir)
    if snapshot_id is None:
        return snapshots[0] if snapshots else None
    return next((item for item in snapshots if item.id == snapshot_id), None)


def restore(
    snapshot_id: str,
    *,
    root: Path,
    state_dir: Path,
    keep: int = DEFAULT_KEEP,
    lock: Callable[[], AbstractContextManager[None]] | None = None,
) -> tuple[bool, str]:
    """Restore one Skill root from a snapshot; return ``(ok, message)``.

    ``lock`` is an optional zero-argument callable returning a context manager; the
    extension passes ``SkillManager.write_scope`` so the whole replace runs under the
    formal Skill lock.
    """
    snapshot = resolve_snapshot(state_dir, snapshot_id)
    if snapshot is None:
        return False, f"no snapshot with id {snapshot_id!r}; use `/curator status` to list them"
    archive = snapshot.path / SNAPSHOT_ARCHIVE
    if not archive.is_file():
        return False, f"snapshot {snapshot.id} has no {SNAPSHOT_ARCHIVE}; it is corrupted"
    with _lock_scope(lock):
        return _restore_locked(snapshot, archive, root=root, state_dir=state_dir, keep=keep)


def _restore_locked(
    snapshot: SnapshotRef,
    archive: Path,
    *,
    root: Path,
    state_dir: Path,
    keep: int,
) -> tuple[bool, str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"could not create {root}: {exc}"
    safety = snapshot_skills(
        root,
        reason=f"pre-restore to {snapshot.id}",
        state_dir=state_dir,
        keep=keep,
        protect={snapshot.id},
    )
    if safety is None:
        return False, "pre-restore snapshot failed; nothing was changed"

    staging = root / f"{STAGING_PREFIX}{utc_id()}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return False, f"could not create {staging}: {exc}"
    moved: list[tuple[Path, Path]] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry == staging or _excluded(entry.name):
                continue
            destination = staging / entry.name
            shutil.move(str(entry), str(destination))
            moved.append((entry, destination))
    except OSError as exc:
        failed = _unstage(moved)
        _drop(staging)
        suffix = f"; not put back by hand: {', '.join(failed)}" if failed else ""
        return False, f"could not stage the current tree ({exc}); nothing was changed{suffix}"

    try:
        with tarfile.open(archive, "r:gz") as handle:
            unsafe = _unsafe_member(handle.getmembers())
            if unsafe is not None:
                raise tarfile.TarError(f"refusing unsafe archive member {unsafe!r}")
            handle.extractall(str(root), filter="data")
    # Every failure mode here is an archive reader exception: gzip raises EOFError on a
    # truncated stream, tarfile raises TarError and a bad filter raises ValueError. The
    # previous tree has to go back whatever the reader complains about.
    except Exception as exc:
        staged_names = {source.name for source, _ in moved}
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry == staging or _excluded(entry.name) or entry.name in staged_names:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                logger.warning("curator could not remove %s after a failed restore", entry)
        failed = _unstage(moved)
        if failed:
            return False, (
                f"snapshot extract failed ({exc}); could not move back "
                f"{', '.join(failed)}; staged copies kept at {staging}"
            )
        _drop(staging)
        return False, f"snapshot extract failed ({exc}); the previous tree was put back"

    _drop(staging)
    plural = "y" if len(moved) == 1 else "ies"
    return True, (
        f"restored {root} from snapshot {snapshot.id} ({len(moved)} entr{plural} replaced); "
        f"the replaced tree, including its `.archive/` and ledger, is kept inside "
        f"pre-restore snapshot {safety.id}"
    )


def count_skill_files(root: Path) -> int:
    """Count the ``SKILL.md`` files a snapshot would capture."""
    try:
        return sum(1 for path in root.rglob("SKILL.md") if not _excluded_in_tree(path, root))
    except OSError:
        return 0


def _excluded_in_tree(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(_excluded(part) for part in relative.parts)


def _excluded(name: str) -> bool:
    return name in EXCLUDE_NAMES or name.startswith(STAGING_PREFIX)


def _unsafe_member(members: list[tarfile.TarInfo]) -> str | None:
    """Return the name of the first unsafe member, or ``None`` when all are safe."""
    for member in members:
        name = member.name.replace("\\", "/")
        parts = [part for part in name.split("/") if part]
        if not parts or name.startswith("/") or Path(name).is_absolute() or ".." in parts:
            return member.name
        if member.issym() or member.islnk() or member.isdev():
            return member.name
    return None


def _unstage(moved: list[tuple[Path, Path]]) -> list[str]:
    """Move staged entries back; return the names that could not be restored."""
    failed: list[str] = []
    for original, staged in moved:
        try:
            if original.is_dir() and not original.is_symlink():
                shutil.rmtree(original)
            elif original.exists() or original.is_symlink():
                original.unlink()
            shutil.move(str(staged), str(original))
        except OSError:
            failed.append(original.name)
    return failed


def _drop(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _free_id(backups: Path, now: datetime | None) -> str:
    base = utc_id(now)
    candidate = base
    counter = 1
    while (backups / candidate).exists():
        counter += 1
        candidate = f"{base}-{counter:02d}"
    return candidate


def _read_snapshot(directory: Path) -> SnapshotRef:
    manifest: dict[str, Any] = {}
    path = directory / SNAPSHOT_MANIFEST
    try:
        if path.stat().st_size <= MAX_MANIFEST_BYTES:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                manifest = raw
    except (OSError, json.JSONDecodeError):
        manifest = {}
    archive = directory / SNAPSHOT_ARCHIVE
    try:
        archive_bytes = archive.stat().st_size
    except OSError:
        archive_bytes = 0
    return SnapshotRef(
        id=_manifest_text(manifest.get("id")) or directory.name,
        path=directory,
        archive=_manifest_text(manifest.get("archive")) or SNAPSHOT_ARCHIVE,
        reason=_manifest_text(manifest.get("reason")) or "",
        created_at=_manifest_text(manifest.get("created_at")) or "",
        archive_bytes=_manifest_int(manifest.get("archive_bytes"), archive_bytes),
        skill_files=_manifest_int(manifest.get("skill_files"), 0),
        root=_manifest_text(manifest.get("root")) or "",
    )


def _manifest_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _manifest_int(value: Any, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return fallback


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextmanager
def _lock_scope(lock: Callable[[], AbstractContextManager[None]] | None) -> Iterator[None]:
    """Enter the caller's lock when one was supplied."""
    if lock is None:
        yield
        return
    with lock():
        yield


__all__ = [
    "BACKUPS_DIR",
    "DEFAULT_KEEP",
    "SNAPSHOT_ARCHIVE",
    "SNAPSHOT_MANIFEST",
    "STAGING_PREFIX",
    "SnapshotRef",
    "backups_dir",
    "count_skill_files",
    "list_snapshots",
    "prune_old",
    "resolve_snapshot",
    "restore",
    "snapshot_skills",
    "utc_id",
]
