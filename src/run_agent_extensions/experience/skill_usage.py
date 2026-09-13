"""Skill usage telemetry and lifecycle state, in a sidecar next to the skills.

Per-skill counters live in ``<skills-dir>/.usage.json`` rather than in the SKILL.md
frontmatter, so operational telemetry never edits user-authored content. The record
also carries the lifecycle state the curator drives (active, stale, archived), the pin
flag that opts a skill out of every automatic transition, and the management marker
that says whether autonomous curation may touch the skill at all.

``created_by`` reads like provenance but is consumed as policy: ``agent`` means the
skill was created by the review fork and is curator-managed; anything else (a hand
written skill, one the user asked the foreground agent to write) is user-owned and
off-limits to autonomous maintenance until the user adopts it explicitly.

Writes are atomic and best-effort: a broken sidecar never breaks the tool call that
tried to bump a counter.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_agent_coding.host.learning import is_agent_created, writeback_enabled

logger = logging.getLogger(__name__)

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
VALID_STATES = frozenset({STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED})
CREATED_BY_AGENT = "agent"
CREATED_BY_INSTALLED = "installed"
ARCHIVE_DIR = ".archive"
USAGE_FILE = ".usage.json"

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def empty_record() -> dict[str, Any]:
    return {
        "created_by": None,
        "use_count": 0,
        "view_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "patch_count": 0,
        "patch_generation": 0,
        "last_reused_patch_generation": 0,
        "last_patched_at": None,
        "created_at": now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
        "last_restored_at": None,
    }


def latest_activity_at(record: dict[str, Any]) -> str | None:
    """The newest of the activity timestamps, or None when the skill was never touched."""
    stamps = [
        parsed
        for key in ("last_used_at", "last_viewed_at", "last_patched_at", "last_restored_at")
        if (parsed := parse_iso(record.get(key))) is not None
    ]
    return max(stamps).isoformat() if stamps else None


def activity_count(record: dict[str, Any]) -> int:
    """Views, uses and patches added together: how much a skill has been touched."""
    return sum(_int(record.get(key)) for key in ("use_count", "view_count", "patch_count"))


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _path_has_redirect(path: Path, root: Path) -> bool:
    current = path.absolute()
    boundary = root.absolute()
    while True:
        try:
            redirected = current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            )
        except OSError:
            redirected = False
        if redirected:
            return True
        if current == boundary or current.parent == current:
            return False
        current = current.parent


class SkillUsage:
    """The sidecar for one skills directory."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self.path = skills_dir / USAGE_FILE
        self.archive_dir = skills_dir / ARCHIVE_DIR

    # -- I/O ----------------------------------------------------------------------

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save(self, data: dict[str, dict[str, Any]]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".usage_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, indent=2, sort_keys=True, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                return True
            except BaseException:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
        except OSError:
            logger.debug("failed to write %s", self.path, exc_info=True)
            return False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:  # pragma: no cover
            yield
            return
        handle = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            handle.close()

    def get(self, name: str) -> dict[str, Any]:
        record = self.load().get(name)
        base = empty_record()
        if isinstance(record, dict):
            base.update(record)
        return base

    def has_record(self, name: str) -> bool:
        return name in self.load()

    def _mutate(self, name: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        if not name:
            return None
        try:
            with self._locked():
                data = self.load()
                record = data.get(name)
                if not isinstance(record, dict):
                    record = empty_record()
                base = empty_record()
                for key, value in base.items():
                    record.setdefault(key, value)
                outcome = mutator(record)
                data[name] = record
                self.save(data)
                return outcome
        except Exception:
            logger.debug("usage mutation for %s failed", name, exc_info=True)
            return None

    # -- counters -------------------------------------------------------------------

    def bump_view(self, name: str) -> None:
        """Count a view, unless an evaluation has disabled all experience writes."""
        if not writeback_enabled() or is_agent_created():
            return

        def apply(record: dict[str, Any]) -> None:
            record["view_count"] = _int(record.get("view_count")) + 1
            record["last_viewed_at"] = now_iso()

        self._mutate(name, apply)

    def bump_use(self, name: str, *, count_patch_reuse: bool = True) -> dict[str, Any] | None:
        """Record a use unless an evaluation has disabled all experience writes.

        The facts returned are the lifecycle fields hermes emits: ``created_by``,
        ``use_count``, ``reused`` and ``reuse_after_patch``.
        """
        if not writeback_enabled() or is_agent_created():
            return None

        def apply(record: dict[str, Any]) -> dict[str, Any]:
            previous = _int(record.get("use_count"))
            generation = _int(record.get("patch_generation"))
            reused_generation = min(_int(record.get("last_reused_patch_generation")), generation)
            reused = previous > 0
            reuse_after_patch = count_patch_reuse and reused and generation > reused_generation
            record["use_count"] = previous + 1
            record["last_used_at"] = now_iso()
            record["patch_generation"] = generation
            record["last_reused_patch_generation"] = reused_generation
            if reuse_after_patch:
                record["last_reused_patch_generation"] = generation
            return {
                "use_count": previous + 1,
                "reused": reused,
                "reuse_after_patch": reuse_after_patch,
            }

        outcome = self._mutate(name, apply)
        return outcome if isinstance(outcome, dict) else None

    def bump_patch(self, name: str) -> None:
        def apply(record: dict[str, Any]) -> None:
            record["patch_count"] = _int(record.get("patch_count")) + 1
            record["patch_generation"] = _int(record.get("patch_generation")) + 1
            record["last_patched_at"] = now_iso()

        self._mutate(name, apply)

    def record_created(self, name: str, *, agent_created: bool) -> None:
        """A successful create is a new logical skill even over stale sidecar state."""

        def apply(record: dict[str, Any]) -> None:
            record.clear()
            record.update(empty_record())
            if agent_created:
                record["created_by"] = CREATED_BY_AGENT

        self._mutate(name, apply)

    def record_installed(self, name: str) -> None:
        """Mark a skill as installed from elsewhere; never curator-managed."""

        def apply(record: dict[str, Any]) -> None:
            record["created_by"] = CREATED_BY_INSTALLED
            record["state"] = STATE_ACTIVE
            record["archived_at"] = None

        self._mutate(name, apply)

    def seed_record_if_missing(self, name: str) -> None:
        """Anchor a skill's inactivity clock to now when it has no record yet."""
        if not name:
            return
        try:
            with self._locked():
                data = self.load()
                if isinstance(data.get(name), dict):
                    return
                data[name] = empty_record()
                self.save(data)
        except Exception:
            logger.debug("seed_record_if_missing(%s) failed", name, exc_info=True)

    def adopt(self, name: str) -> None:
        """Opt a user-owned skill into curator management (hermes ``mark_agent_created``)."""

        def apply(record: dict[str, Any]) -> None:
            record["created_by"] = CREATED_BY_AGENT

        self._mutate(name, apply)

    mark_agent_created = adopt

    def set_state(self, name: str, state: str) -> dict[str, Any] | None:
        """Move the lifecycle state; returns what changed, or None for an invalid state."""
        if state not in VALID_STATES:
            logger.debug("set_state: invalid state %r for %s", state, name)
            return None

        def apply(record: dict[str, Any]) -> dict[str, Any]:
            previous = record.get("state")
            if previous == state:
                return {"changed": False, "created_by": record.get("created_by")}
            record["state"] = state
            if state == STATE_ARCHIVED:
                record["archived_at"] = now_iso()
            elif state == STATE_ACTIVE:
                record["archived_at"] = None
            return {
                "changed": True,
                "created_by": record.get("created_by"),
                "previous_state": previous,
            }

        outcome = self._mutate(name, apply)
        return outcome if isinstance(outcome, dict) else None

    def set_pinned(self, name: str, pinned: bool) -> None:
        def apply(record: dict[str, Any]) -> None:
            record["pinned"] = bool(pinned)

        self._mutate(name, apply)

    def forget(self, name: str) -> None:
        try:
            with self._locked():
                data = self.load()
                if data.pop(name, None) is not None:
                    self.save(data)
        except Exception:
            logger.debug("forget %s failed", name, exc_info=True)

    # -- policy -------------------------------------------------------------------

    def is_curator_managed(self, name: str) -> bool:
        """Whether autonomous curation may mutate or archive this skill.

        A missing record and an explicit ``created_by: null`` resolve identically: not
        managed. Adoption is the supported way in.
        """
        return is_curator_managed_record(self.load().get(name))

    def is_pinned(self, name: str) -> bool:
        return bool(self.get(name).get("pinned"))

    def _skill_dir(self, name: str) -> Path:
        direct = self.skills_dir / name
        if _path_has_redirect(direct, self.skills_dir):
            return direct
        if (direct / "SKILL.md").is_file():
            return direct
        if not self.skills_dir.is_dir():
            return direct
        for category in sorted(self.skills_dir.iterdir(), key=lambda item: item.name):
            if (
                category.name.startswith(".")
                or not category.is_dir()
                or _path_has_redirect(category, self.skills_dir)
            ):
                continue
            nested = category / name
            if (
                not _path_has_redirect(nested, self.skills_dir)
                and (nested / "SKILL.md").is_file()
                and not (category / "SKILL.md").is_file()
            ):
                return nested
        return direct

    def is_curation_eligible(self, name: str) -> bool:
        """Whether the curator may track this skill at all: it exists on disk here.

        hermes also excludes hub-installed, bundled and external skills; this project has
        none of those, so every local skill directory is eligible and only ``pinned``
        and the management marker gate what happens to it.
        """
        directory = self._skill_dir(name)
        return (
            bool(name)
            and not _path_has_redirect(directory, self.skills_dir)
            and (directory / "SKILL.md").is_file()
        )

    def provenance(self, name: str) -> str:
        """``installed`` when marked so, else ``agent`` (local, whoever wrote it)."""
        record = self.load().get(name)
        if isinstance(record, dict) and record.get("created_by") == CREATED_BY_INSTALLED:
            return "installed"
        return "agent"

    # -- archive ------------------------------------------------------------------

    def archive(self, name: str) -> tuple[bool, str]:
        """Move a skill directory into ``.archive/``; recoverable, unlike a delete."""
        source = self._skill_dir(name)
        if _path_has_redirect(source, self.skills_dir):
            return False, f"skill {name!r} uses a symlink or junction"
        if not (source / "SKILL.md").is_file():
            return False, f"skill {name!r} not found"
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"failed to create archive dir: {exc}"
        destination = self.archive_dir / name
        if destination.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            destination = self.archive_dir / f"{name}-{stamp}"
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            return False, f"failed to archive: {exc}"
        self.set_state(name, STATE_ARCHIVED)
        return True, f"archived to {destination}"

    def restore(self, name: str) -> tuple[bool, str]:
        if not self.archive_dir.is_dir():
            return False, "no archive directory"
        candidates = [p for p in self.archive_dir.iterdir() if p.is_dir() and p.name == name]
        if not candidates:
            prefix = f"{name}-"
            candidates = sorted(
                (
                    p
                    for p in self.archive_dir.iterdir()
                    if p.is_dir()
                    and p.name.startswith(prefix)
                    and len(p.name) - len(prefix) == 14
                    and p.name[len(prefix) :].isdigit()
                ),
                reverse=True,
            )
        if not candidates:
            return False, f"skill {name!r} not found in archive"
        destination = self.skills_dir / name
        if destination.exists():
            return False, f"destination already exists: {destination}"
        try:
            shutil.move(str(candidates[0]), str(destination))
        except OSError as exc:
            return False, f"failed to restore: {exc}"
        self.set_state(name, STATE_ACTIVE)
        self._mutate(name, lambda record: record.update(last_restored_at=now_iso()))
        return True, f"restored to {destination}"

    def archived_names(self) -> list[str]:
        if not self.archive_dir.is_dir():
            return []
        return sorted(p.name for p in self.archive_dir.iterdir() if (p / "SKILL.md").is_file())

    # -- reporting ----------------------------------------------------------------

    def _row(self, name: str, raw: object) -> dict[str, Any]:
        base = empty_record()
        if isinstance(raw, dict):
            base.update(raw)
        row: dict[str, Any] = {"name": name, **base}
        row["_persisted"] = isinstance(raw, dict)
        row["has_record"] = isinstance(raw, dict)
        row["has_provenance_key"] = isinstance(raw, dict) and "created_by" in raw
        row["last_activity_at"] = latest_activity_at(base)
        row["activity_count"] = activity_count(base)
        row["provenance"] = self.provenance(name)
        return row

    def managed_report(self, present: list[str]) -> list[dict[str, Any]]:
        """One row per curator-managed skill that exists on disk (hermes ``curated_report``)."""
        data = self.load()
        return [
            self._row(name, data.get(name))
            for name in present
            if is_curator_managed_record(data.get(name))
        ]

    curated_report = managed_report

    def unmanaged_report(self, present: list[str]) -> list[dict[str, Any]]:
        """Skills the curator could manage but never will until the user adopts them.

        Provenance is a declaration, never an inference: heavy use or patch counts are
        evidence of maintenance, not of authorship, so nothing here auto-adopts.
        """
        data = self.load()
        return [
            self._row(name, data.get(name))
            for name in present
            if not is_curator_managed_record(data.get(name))
            and not (
                isinstance(data.get(name), dict)
                and data[name].get("created_by") == CREATED_BY_INSTALLED
            )
        ]

    def usage_report(self, present: list[str]) -> list[dict[str, Any]]:
        """Telemetry for every skill on disk, managed or not."""
        data = self.load()
        return sorted(
            (self._row(name, data.get(name)) for name in present), key=lambda r: r["name"]
        )


def is_curator_managed_record(record: object) -> bool:
    """``created_by: agent`` is a management opt-in flag, not proof of authorship."""
    if not isinstance(record, dict):
        return False
    return record.get("created_by") == CREATED_BY_AGENT or record.get("agent_created") is True


__all__ = [
    "ARCHIVE_DIR",
    "CREATED_BY_AGENT",
    "CREATED_BY_INSTALLED",
    "STATE_ACTIVE",
    "STATE_ARCHIVED",
    "STATE_STALE",
    "USAGE_FILE",
    "SkillUsage",
    "activity_count",
    "empty_record",
    "is_curator_managed_record",
    "latest_activity_at",
    "now_iso",
    "parse_iso",
]
