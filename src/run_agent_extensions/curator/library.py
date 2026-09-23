"""The Skill library the Curator maintains: records, protection and archiving.

The library is a read-mostly view over the two formal Skill roots
(``<home>/skills`` and ``<cwd>/.run/skills``) built on the existing read-only
``SkillManager``. It adds exactly three things the Curator needs:

* a per-Skill record joining the file state (digest, mtime, ``created_by``,
  pinned flag) with the Curator's own ledger and consultation log;
* the protection rules that decide which records may be *archived* automatically;
* archive and restore, which move the whole Skill directory to
  ``<skills-root>/.archive/<name>`` and back.

Nothing here ever deletes Skill content: archiving is a directory move, recorded
in the existing Skill ledger with actor ``evolution`` (the only non-human actor
``VALID_ACTORS`` accepts) and the reason in its evidence.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from run_agent_extensions.experience.scopes import Scope
from run_agent_extensions.experience.skill_manager import EVOLUTION_OWNER, SkillManager
from run_agent_extensions.experience.skill_usage import ARCHIVE_DIR

from .config import CuratorConfig
from .state import CuratorStateStore, parse_epoch, record_key, utc_now

logger = logging.getLogger(__name__)

# The only actor ``skill_ledger.VALID_ACTORS`` accepts for an automatic pass.
LEDGER_ACTOR = "evolution"
ARCHIVE_TIMESTAMP = "%Y%m%d%H%M%S"
SCOPES: tuple[Scope, ...] = ("user", "project")


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """One discovered Skill plus the Curator's own knowledge about it."""

    name: str
    scope: Scope
    path: Path
    digest: str
    created_by: str
    pinned: bool
    mtime: float
    last_mutation_at: float | None
    last_consulted_at: float | None
    state: str

    @property
    def key(self) -> str:
        """Return ``<scope>/<name>``, the Curator's identity for this Skill."""
        return record_key(self.scope, self.name)

    @property
    def anchor(self) -> float:
        """Return the activity timestamp every transition is measured against."""
        return self.last_consulted_at or self.last_mutation_at or self.mtime

    def as_json(self) -> dict[str, Any]:
        """Return the record as it appears in a review prompt or a report."""
        return {
            "name": self.name,
            "scope": self.scope,
            "path": str(self.path),
            "digest": self.digest,
            "created_by": self.created_by,
            "pinned": self.pinned,
            "state": self.state,
            "mtime": self.mtime,
            "last_mutation_at": self.last_mutation_at,
            "last_consulted_at": self.last_consulted_at,
        }


@dataclass(frozen=True, slots=True)
class LibraryMutation:
    """The outcome of one archive or restore."""

    ok: bool
    message: str
    path: Path | None = None
    ledger_id: str | None = None
    moved_as: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryView:
    """Records plus their bodies, captured once for one pass."""

    records: tuple[SkillRecord, ...] = ()
    bodies: dict[str, str] = field(default_factory=dict)

    def by_name(self, name: str) -> SkillRecord | None:
        """Return the record for ``name``, preferring the project scope."""
        for scope in ("project", "user"):
            for record in self.records:
                if record.name == name and record.scope == scope:
                    return record
        return None


class CuratorLibrary:
    """Records, protection rules, archive and restore for the formal Skill roots."""

    def __init__(
        self,
        manager: SkillManager,
        store: CuratorStateStore,
        *,
        config: CuratorConfig,
        project_enabled: bool = True,
        now: datetime | None = None,
    ) -> None:
        self.manager = manager
        self.store = store
        self.config = config
        self.project_enabled = project_enabled
        self._now = now

    # -- discovery ----------------------------------------------------------------

    @property
    def roots(self) -> tuple[tuple[Scope, Path], ...]:
        """Return the Skill roots this session may touch."""
        return tuple((scope, self.manager.roots.directory(scope)) for scope in self.scopes())

    def scopes(self) -> tuple[Scope, ...]:
        """Return the scopes eligible in this session."""
        return tuple(scope for scope in SCOPES if scope == "user" or self.project_enabled)

    def records(self) -> tuple[SkillRecord, ...]:
        """Return every discoverable Skill record, ordered by scope then name."""
        moment = self._now or utc_now()
        lookback = moment.timestamp() - self.config.usage_lookback_days * 86_400.0
        found: list[SkillRecord] = []
        for scope in self.scopes():
            for info in self.manager.describe(scope):
                if info.path is None:
                    continue
                content = self.manager.main_content(scope, info.name)
                if content is None:
                    continue
                found.append(
                    SkillRecord(
                        name=info.name,
                        scope=scope,
                        path=info.path,
                        digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                        created_by=info.created_by,
                        pinned=info.pinned,
                        mtime=_mtime(info.path / "SKILL.md"),
                        last_mutation_at=self.last_mutation_at(scope, info.name),
                        last_consulted_at=self.store.last_consulted_at(
                            info.name, scope, not_before=lookback
                        ),
                        state=self.store.state_for(scope, info.name).state,
                    )
                )
        return tuple(found)

    def view(self) -> LibraryView:
        """Return records plus every ``SKILL.md`` body read once."""
        records = self.records()
        bodies: dict[str, str] = {}
        for record in records:
            body = self.manager.main_content(record.scope, record.name)
            if body is not None:
                bodies[record.key] = body
        return LibraryView(records, bodies)

    def find_by_name(self, name: str) -> SkillRecord | None:
        """Return one record by name, preferring the project scope."""
        return self.view().by_name(name)

    def scope_of(self, name: str) -> Scope:
        """Return the scope a named Skill lives in, defaulting to ``user``."""
        record = self.find_by_name(name)
        return record.scope if record is not None else "user"

    def last_mutation_at(self, scope: Scope, name: str) -> float | None:
        """Return the timestamp of the newest *content* mutation of one Skill."""
        try:
            entries = self.manager.ledger[scope].entries(skill=name, limit=25)
        except Exception:  # a damaged ledger must not break discovery
            logger.debug("curator could not read the %s ledger for %s", scope, name, exc_info=True)
            return None
        # An archive entry records that the Skill was moved away, not that anyone used
        # it, so it never becomes the activity anchor of a same-named Skill that appears
        # later. A restore entry does count: the Skill is back in the live root.
        entry = next((item for item in entries if item.action != "archive"), None)
        return parse_epoch(entry.timestamp) if entry is not None else None

    # -- protection ---------------------------------------------------------------

    def protected(self, record: SkillRecord) -> str | None:
        """Return why ``record`` must never be archived automatically."""
        if record.pinned:
            return "pinned"
        if record.name in self.config.protected_names:
            return "protected name"
        if record.scope == "project":
            if not self.project_enabled:
                return "project inputs are untrusted"
            if not self.config.auto_archive_project_skills:
                return "project scope is not auto-archived"
        if record.created_by != EVOLUTION_OWNER and not self.config.auto_archive_user_skills:
            return "user-written skill is not auto-archived"
        return None

    def transition_block(self, record: SkillRecord) -> str | None:
        """Return why *every* automatic transition is skipped for ``record``.

        A pinned Skill and a protected name bypass maintenance entirely, exactly as
        they do in hermes. Everything else is still eligible for a stale marker: a
        user-written Skill may be reported as unused before a human decides.
        """
        if record.pinned:
            return "pinned"
        if record.name in self.config.protected_names:
            return "protected name"
        if record.scope == "project" and not self.project_enabled:
            return "project inputs are untrusted"
        return None

    def skip_reason(self, record: SkillRecord) -> str | None:
        """Return the single reason an automatic archive is refused."""
        return self.transition_block(record) or self.protected(record)

    # -- state ---------------------------------------------------------------------

    def mark(self, record: SkillRecord, state: str, *, now: datetime | None = None) -> None:
        """Store one record's lifecycle state."""
        self.store.set_state(record.scope, record.name, state, since=now or self._now or utc_now())

    # -- archive / restore ---------------------------------------------------------

    def archive_root(self, scope: Scope) -> Path:
        """Return the ``.archive`` directory for one scope."""
        return self.manager.roots.directory(scope) / ARCHIVE_DIR

    def archive(
        self, record: SkillRecord, *, reason: str, now: datetime | None = None
    ) -> LibraryMutation:
        """Move one whole Skill directory into ``<skills-root>/.archive``."""
        moment = now or self._now or utc_now()
        with self.manager.write_scope(record.scope):
            directory = self.manager.find(record.scope, record.name)
            if directory is None:
                return LibraryMutation(False, f"{record.key} is no longer present")
            root = self.manager.roots.directory(record.scope)
            archive_root = self.archive_root(record.scope)
            try:
                archive_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return LibraryMutation(False, f"could not create {archive_root}: {exc}")
            destination = _archive_destination(archive_root, record.name, moment)
            ledger = self.manager.ledger[record.scope]
            before = ledger.capture_before(directory)
            try:
                _move(directory, destination)
            except OSError as exc:
                return LibraryMutation(False, f"could not archive {record.key}: {exc}")
            ledger_id = ledger.record(
                "archive",
                record.name,
                actor=LEDGER_ACTOR,
                before=before,
                after_root=destination,
                evidence={
                    "reason": reason,
                    "scope": record.scope,
                    "source": str(directory),
                    "root": str(root),
                    "digest": record.digest,
                },
            )
        self.mark(record, "archived", now=moment)
        return LibraryMutation(
            True,
            f"archived {record.key} to {destination.relative_to(root)}",
            destination,
            ledger_id,
            destination.name,
        )

    def archived_names(self, scope: Scope) -> tuple[str, ...]:
        """Return the archived directory names in one scope, newest first."""
        root = self.archive_root(scope)
        try:
            names = [item.name for item in root.iterdir() if item.is_dir()]
        except OSError:
            return ()
        return tuple(sorted(names, reverse=True))

    def restore(self, scope: Scope, name: str, *, now: datetime | None = None) -> LibraryMutation:
        """Move an archived whole-Skill directory back into the live root."""
        moment = now or self._now or utc_now()
        root = self.manager.roots.directory(scope)
        source = _archived_directory(self.archive_root(scope), name)
        if source is None:
            return LibraryMutation(False, f"no archived skill named {name!r} in the {scope} scope")
        destination = root / name
        if destination.exists():
            return LibraryMutation(False, f"{scope}/{name} already exists; refusing to overwrite")
        with self.manager.write_scope(scope):
            ledger = self.manager.ledger[scope]
            before = ledger.capture_before(source)
            try:
                _move(source, destination)
            except OSError as exc:
                return LibraryMutation(False, f"could not restore {name!r}: {exc}")
            ledger_id = ledger.record(
                "restore",
                name,
                actor=LEDGER_ACTOR,
                before=before,
                after_root=destination,
                evidence={"scope": scope, "source": str(source), "root": str(root)},
            )
        self.store.set_state(scope, name, "active", since=moment)
        return LibraryMutation(True, f"restored {scope}/{name}", destination, ledger_id, name)

    # -- snapshots -----------------------------------------------------------------

    def scope_for_root(self, root: Path) -> Scope | None:
        """Return the scope owning ``root``, or ``None`` when it is not a Skill root."""
        for scope in SCOPES:
            if self.manager.roots.directory(scope) == root:
                return scope
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _move(source: Path, destination: Path) -> None:
    """Rename one directory, falling back to ``shutil.move`` across devices."""
    try:
        source.rename(destination)
    except OSError:
        shutil.move(str(source), str(destination))


def _archive_destination(archive_root: Path, name: str, now: datetime) -> Path:
    """Return a free ``<name>[-<timestamp>]`` path under ``.archive``."""
    direct = archive_root / name
    if not direct.exists():
        return direct
    stamp = now.strftime(ARCHIVE_TIMESTAMP)
    candidate = archive_root / f"{name}-{stamp}"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = archive_root / f"{name}-{stamp}-{counter:02d}"
    return candidate


def _archived_directory(archive_root: Path, name: str) -> Path | None:
    """Find an archived directory for ``name``, newest first.

    Only the exact name or the ``<name>-<YYYYMMDDHHMMSS>[-NN]`` shape archive writes
    match, so restoring ``git`` can never pull an unrelated ``git-helpers`` copy out
    of the archive.
    """
    direct = archive_root / name
    if direct.is_dir():
        return direct
    pattern = re.compile(rf"^{re.escape(name)}-\d{{14}}(-\d{{2}})?$")
    try:
        matches = [
            item for item in archive_root.iterdir() if item.is_dir() and pattern.match(item.name)
        ]
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda item: _mtime(item))


__all__ = [
    "ARCHIVE_TIMESTAMP",
    "LEDGER_ACTOR",
    "SCOPES",
    "CuratorLibrary",
    "LibraryMutation",
    "LibraryView",
    "SkillRecord",
]
