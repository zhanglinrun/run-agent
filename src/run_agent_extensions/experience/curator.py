"""The curator: periodic maintenance of the skills the agent created.

Modelled on hermes-agent's curator. Two passes, kept separate:

1. Automatic transitions need no model. Each curator-managed skill's latest activity
   timestamp (use, view or patch) moves it between ``active``, ``stale`` and
   ``archived``. A never-used skill gets a grace period before it can age out, a pinned
   skill is never touched, and archiving is a move into ``.archive/`` so it is always
   recoverable; nothing is ever hard-deleted autonomously.
2. Consolidation asks the model, and is off by default. It shows the candidate list
   and asks for an umbrella-building plan: patch or create class-level skills, demote
   narrow siblings into support files, archive what was absorbed. Every archive must
   name the umbrella that absorbed it; a bare prune is refused.

The curator runs when the agent has been idle for a while and the last run is older
than the interval. State (``last_run_at``, ``run_count``, ``paused``) lives in a small
JSON file next to the skills; each run writes a report so the user can see what
changed and roll back a single edit through the ledger if a decision was wrong.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from run_agent_coding.host.learning import require_writeback, review_origin, writeback_enabled
from run_agent_coding.host.process_identity import process_identity

from .config import ExperienceConfig
from .memory import MemoryScope
from .mutation import require_mutation
from .skill_backup import SkillBackups
from .skill_manager import SkillManager
from .skill_usage import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, parse_iso

logger = logging.getLogger(__name__)

STATE_FILE = ".curator_state.json"
REPORT_DIR = ".curator_reports"


@dataclass(slots=True)
class CuratorLease:
    """Root-level lease preventing two sessions from curating one Skill library."""

    path: Path
    _held: bool = False

    def acquire(self) -> bool:
        if self._held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._stale():
                return False
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                self.path.unlink()
            raise
        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            same_owner = int(record.get("pid")) == os.getpid() and record.get(
                "identity"
            ) == process_identity(os.getpid())
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return
        if same_owner:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def _stale(self) -> bool:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(record["pid"])
            identity = record.get("identity")
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return False
        if pid == os.getpid():
            return False
        try:
            live = process_identity(pid)
        except (OSError, ValueError):
            return True
        return live is None or (isinstance(identity, str) and identity != live)


Ask = Callable[[str, str], Awaitable[str]]
_LIFECYCLE_FIELDS = (
    "created_by",
    "state",
    "pinned",
    "use_count",
    "last_used_at",
    "last_viewed_at",
    "last_patched_at",
    "last_restored_at",
    "patch_generation",
)


def _lifecycle_changed(snapshot: dict[str, Any], current: dict[str, Any]) -> bool:
    return any(snapshot.get(field) != current.get(field) for field in _LIFECYCLE_FIELDS)


CURATOR_SYSTEM_PROMPT = (
    "You are the background skill curator. This is an umbrella-building consolidation "
    "pass over the skills the agent itself created, not a passive audit.\n\n"
    "The goal is a library of class-level skills with rich SKILL.md bodies and "
    "references/, templates/ and scripts/ support files, not a long flat list of "
    "one-session-one-skill entries. Judge overlap on content, never on use counts.\n\n"
    "Hard rules: never touch a skill marked pinned; never archive a skill without "
    "naming the umbrella that absorbed its content; never archive a never-used skill "
    "younger than 30 days; keep is legitimate only for a skill that is already a "
    "class-level umbrella.\n\n"
    "Answer with JSON only, no prose and no code fence:\n"
    '{"patches": [{"scope": "project"|"user", "name": "umbrella", "old_text": "...", '
    '"new_text": "..."}], '
    '"creates": [{"scope": "project"|"user", "name": "umbrella", "description": "one '
    'line", "body": "full SKILL.md body"}], '
    '"support_files": [{"scope": "project"|"user", "name": "umbrella", "file_path": '
    '"references/<topic>.md", "content": "..."}], '
    '"archives": [{"scope": "project"|"user", "name": "narrow-skill", "absorbed_into": '
    '"umbrella", "reason": "one sentence"}], '
    '"keep": [{"name": "...", "reason": "..."}]}\n'
    "A patch must quote old_text exactly as it appears in the skill body shown to you. "
    'Answer {"patches": [], "creates": [], "support_files": [], "archives": [], "keep": []} '
    "when nothing should change."
)


@dataclass(slots=True)
class CuratorState:
    last_run_at: str | None = None
    run_count: int = 0
    paused: bool = False
    last_summary: str = ""
    last_report: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "run_count": self.run_count,
            "paused": self.paused,
            "last_summary": self.last_summary,
            "last_report": self.last_report,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CuratorState:
        return cls(
            last_run_at=data.get("last_run_at"),
            run_count=int(data.get("run_count") or 0),
            paused=bool(data.get("paused")),
            last_summary=str(data.get("last_summary") or ""),
            last_report=data.get("last_report"),
        )


@dataclass(frozen=True, slots=True)
class CuratorRun:
    started_at: str
    transitions: dict[str, int]
    consolidation: dict[str, Any]
    report_path: Path | None
    dry_run: bool = False

    @property
    def summary(self) -> str:
        parts = [f"{k.replace('_', ' ')}={v}" for k, v in self.transitions.items() if v]
        auto = ", ".join(parts) or "no transitions"
        if self.consolidation.get("skipped"):
            return f"{auto}; consolidation skipped ({self.consolidation['skipped']})"
        applied = self.consolidation.get("applied", [])
        refused = self.consolidation.get("refused", [])
        return f"{auto}; consolidation applied {len(applied)}, refused {len(refused)}"


@dataclass(slots=True)
class Curator:
    manager: SkillManager
    config: ExperienceConfig
    state_dir: Path
    clock: Callable[[], float] = time.time
    project_enabled: bool = True
    backups: SkillBackups | None = None
    _state: CuratorState = field(default_factory=CuratorState)

    def __post_init__(self) -> None:
        self._state = self._load_state()

    # -- state ----------------------------------------------------------------------

    @property
    def state(self) -> CuratorState:
        return self._state

    def _load_state(self) -> CuratorState:
        path = self.state_dir / STATE_FILE
        if not path.is_file():
            return CuratorState()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CuratorState()
        return CuratorState.from_dict(data if isinstance(data, dict) else {})

    def _save_state(self) -> None:
        path = self.state_dir / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state.to_dict(), indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def set_paused(self, paused: bool) -> None:
        require_writeback()
        self._state.paused = paused
        self._save_state()

    def should_run(self, *, idle_seconds: float | None = None) -> bool:
        """The static gates; the first sighting seeds the clock and waits one interval."""
        if not writeback_enabled():
            return False
        if not self.config.curator_enabled or self._state.paused:
            return False
        if idle_seconds is not None and idle_seconds < self.config.curator_min_idle_seconds:
            return False
        now = datetime.fromtimestamp(self.clock(), tz=UTC)
        last = parse_iso(self._state.last_run_at)
        if last is None:
            self._state.last_run_at = now.isoformat()
            self._state.last_summary = "seeded; first run after one interval"
            self._save_state()
            return False
        return now - last >= timedelta(hours=self.config.curator_interval_hours)

    # -- pass 1: automatic transitions -------------------------------------------------

    def apply_transitions(self, *, dry_run: bool = False) -> dict[str, int]:
        if not dry_run and self.config.skills_write_approval:
            return {
                "checked": 0,
                "marked_stale": 0,
                "archived": 0,
                "reactivated": 0,
            }
        now = datetime.fromtimestamp(self.clock(), tz=UTC)
        stale_cutoff = now - timedelta(days=self.config.curator_stale_after_days)
        archive_cutoff = now - timedelta(days=self.config.curator_archive_after_days)
        counts = {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0}
        for scope in ("user", "project"):
            if scope == "project" and not self.project_enabled:
                continue
            usage = self.manager.usage[scope]
            names = [info.name for info in self.manager.describe(scope)]
            for row in usage.managed_report(names):
                counts["checked"] += 1
                if row.get("pinned"):
                    continue
                name = str(row["name"])
                anchor = (
                    parse_iso(row.get("last_activity_at"))
                    or parse_iso(row.get("created_at"))
                    or now
                )
                current = str(row.get("state") or STATE_ACTIVE)
                never_used = int(row.get("use_count") or 0) == 0
                if never_used and anchor > stale_cutoff:
                    if current == STATE_STALE and not dry_run:
                        with self.manager.write_scope(scope):
                            if _lifecycle_changed(row, usage.get(name)):
                                continue
                            require_mutation("curator")
                            usage.set_state(name, STATE_ACTIVE)
                    if current == STATE_STALE:
                        counts["reactivated"] += 1
                    continue
                if anchor <= archive_cutoff and current != STATE_ARCHIVED:
                    if not dry_run:
                        with self.manager.write_scope(scope):
                            if _lifecycle_changed(row, usage.get(name)):
                                continue
                            require_mutation("curator")
                            before = self.manager.ledger[scope].capture_before(
                                self.manager.find(scope, name) or usage.skills_dir / name
                            )
                            ok, _ = usage.archive(name)
                            if ok:
                                self.manager.ledger[scope].record(
                                    "archive",
                                    name,
                                    actor="curator",
                                    before=before,
                                    after_root=usage.archive_dir / name,
                                    evidence={"reason": "inactive past the archive threshold"},
                                )
                            else:
                                continue
                    counts["archived"] += 1
                elif anchor <= stale_cutoff and current == STATE_ACTIVE:
                    if not dry_run:
                        with self.manager.write_scope(scope):
                            if _lifecycle_changed(row, usage.get(name)):
                                continue
                            require_mutation("curator")
                            usage.set_state(name, STATE_STALE)
                    counts["marked_stale"] += 1
                elif anchor > stale_cutoff and current == STATE_STALE:
                    if not dry_run:
                        with self.manager.write_scope(scope):
                            if _lifecycle_changed(row, usage.get(name)):
                                continue
                            require_mutation("curator")
                            usage.set_state(name, STATE_ACTIVE)
                    counts["reactivated"] += 1
        return counts

    # -- pass 2: consolidation -------------------------------------------------------

    def candidates(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in ("user", "project"):
            if scope == "project" and not self.project_enabled:
                continue
            usage = self.manager.usage[scope]
            names = [info.name for info in self.manager.describe(scope)]
            infos = {info.name: info for info in self.manager.describe(scope)}
            for row in usage.managed_report(names):
                info = infos[str(row["name"])]
                rows.append(
                    {
                        "scope": scope,
                        "name": info.name,
                        "description": info.description,
                        "state": row.get("state"),
                        "pinned": bool(row.get("pinned")),
                        "use_count": int(row.get("use_count") or 0),
                        "last_activity_at": row.get("last_activity_at"),
                        "created_at": row.get("created_at"),
                        "created_by": row.get("created_by"),
                        "last_used_at": row.get("last_used_at"),
                        "last_viewed_at": row.get("last_viewed_at"),
                        "last_patched_at": row.get("last_patched_at"),
                        "last_restored_at": row.get("last_restored_at"),
                        "patch_generation": row.get("patch_generation"),
                    }
                )
        return rows

    def build_prompt(self, candidates: list[dict[str, Any]], *, dry_run: bool = False) -> str:
        lines = ["Candidate skills (curator-managed only):", ""]
        for row in candidates:
            flags = []
            if row["pinned"]:
                flags.append("pinned")
            flags.append(str(row["state"]))
            lines.append(
                f"- {row['scope']}/{row['name']} [{', '.join(flags)}; uses={row['use_count']}; "
                f"last activity={row['last_activity_at'] or 'never'}]: {row['description']}"
            )
            try:
                with self.manager.write_scope(row["scope"]):
                    body = self.manager.view(row["scope"], row["name"], count_usage=False)
            except Exception:
                body = "(unreadable)"
            lines.append("  SKILL.md:")
            lines.extend(f"    {line}" for line in body.splitlines()[:80])
        return "\n".join(lines)

    async def consolidate(self, ask: Ask, *, dry_run: bool = False) -> dict[str, Any]:
        """Ask for a plan and apply it under the review origin, so every guard applies."""
        self.manager.actor_override = "curator"
        try:
            with review_origin():
                self.manager.read_marks.reset()
                return await self._consolidate(ask, dry_run=dry_run)
        finally:
            self.manager.actor_override = None

    async def _consolidate(self, ask: Ask, *, dry_run: bool = False) -> dict[str, Any]:
        if not dry_run and self.config.skills_write_approval:
            return {
                "skipped": "skill write requires explicit approval",
                "applied": [],
                "refused": [],
            }
        candidates = self.candidates()
        candidate_snapshot = {(str(row["scope"]), str(row["name"])): row for row in candidates}

        def with_snapshot(item: dict[str, Any]) -> dict[str, Any]:
            key = (str(item.get("scope")), str(item.get("name")))
            row = candidate_snapshot.get(key)
            return {**item, "_lifecycle": row} if row is not None else item

        if len([c for c in candidates if not c["pinned"]]) < 2:
            return {"skipped": "fewer than two unpinned candidates", "applied": [], "refused": []}
        answer = await ask(CURATOR_SYSTEM_PROMPT, self.build_prompt(candidates, dry_run=dry_run))
        plan = _parse_plan(answer)
        if plan is None:
            return {
                "skipped": "unparseable plan",
                "applied": [],
                "refused": [],
                "raw": answer[:500],
            }
        if dry_run:
            return {"skipped": "dry run", "plan": plan, "applied": [], "refused": []}
        applied: list[str] = []
        refused: list[str] = []
        for patch in plan.get("patches", []):
            self._apply(
                applied,
                refused,
                "patch",
                with_snapshot(patch),
                lambda p: self.manager.patch(
                    p["scope"], p["name"], "SKILL.md", p["old_text"], p["new_text"]
                ),
            )
        for create in plan.get("creates", []):
            self._apply(
                applied,
                refused,
                "create",
                create,
                lambda c: self.manager.create(c["scope"], c["name"], c["description"], c["body"]),
            )
        for support in plan.get("support_files", []):
            self._apply(
                applied,
                refused,
                "write_file",
                with_snapshot(support),
                lambda s: self.manager.write_file(
                    s["scope"], s["name"], s["file_path"], s["content"]
                ),
            )
        for archive in plan.get("archives", []):
            self._apply(
                applied,
                refused,
                "archive",
                with_snapshot(archive),
                lambda a: self.manager.delete(
                    a["scope"], a["name"], absorbed_into=a.get("absorbed_into")
                ),
            )
        return {"applied": applied, "refused": refused, "keep": plan.get("keep", [])}

    def _apply(
        self,
        applied: list[str],
        refused: list[str],
        action: str,
        item: dict[str, Any],
        call: Callable[[dict[str, Any]], Any],
    ) -> None:
        label = f"{action} {item.get('scope', '?')}/{item.get('name', '?')}"
        try:
            scope_value = item.get("scope")
            if scope_value not in ("user", "project"):
                raise ValueError(f"invalid scope: {scope_value!r}")
            scope = cast(MemoryScope, scope_value)
            scopes: tuple[MemoryScope, ...] = (
                ("user", "project") if action == "create" and self.project_enabled else (scope,)
            )
            with self.manager.write_scope(*scopes):
                snapshot = item.get("_lifecycle")
                if isinstance(snapshot, dict) and _lifecycle_changed(
                    snapshot, self.manager.usage[scope].get(str(item.get("name")))
                ):
                    refused.append(f"{label}: skipped because the skill changed during planning")
                    return
                require_mutation("curator")
                result = call(item)
        except Exception as exc:
            refused.append(f"{label}: {type(exc).__name__}: {exc}")
            return
        applied.append(f"{label}: {getattr(result, 'message', result)}")

    # -- a whole run ------------------------------------------------------------------

    async def run(
        self, ask: Ask | None, *, dry_run: bool = False, consolidate: bool | None = None
    ) -> CuratorRun:
        lease = CuratorLease(self.state_dir / ".curator.lock")
        if not lease.acquire():
            transitions = {
                "checked": 0,
                "marked_stale": 0,
                "archived": 0,
                "reactivated": 0,
            }
            consolidation = {
                "skipped": "curator lease held by another session",
                "applied": [],
                "refused": [],
            }
            return CuratorRun(
                datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
                transitions,
                consolidation,
                None,
                dry_run,
            )
        try:
            return await self._run_locked(ask, dry_run=dry_run, consolidate=consolidate)
        finally:
            lease.release()

    async def _run_locked(
        self, ask: Ask | None, *, dry_run: bool = False, consolidate: bool | None = None
    ) -> CuratorRun:
        if not dry_run:
            require_mutation("curator")
            if self.config.skills_write_approval:
                return CuratorRun(
                    datetime.fromtimestamp(self.clock(), tz=UTC).isoformat(),
                    {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0},
                    {
                        "skipped": "skill write requires explicit approval",
                        "applied": [],
                        "refused": [],
                    },
                    None,
                    dry_run,
                )
            if self.backups is not None:
                self.backups.snapshot("curator run")
        started = datetime.fromtimestamp(self.clock(), tz=UTC)
        transitions = self.apply_transitions(dry_run=dry_run)
        do_consolidate = self.config.curator_consolidate if consolidate is None else consolidate
        if not do_consolidate:
            consolidation: dict[str, Any] = {
                "skipped": "consolidation disabled",
                "applied": [],
                "refused": [],
            }
        elif ask is None:
            consolidation = {"skipped": "no model available", "applied": [], "refused": []}
        else:
            try:
                consolidation = await self.consolidate(ask, dry_run=dry_run)
            except Exception as exc:
                consolidation = {
                    "skipped": f"{type(exc).__name__}: {exc}",
                    "applied": [],
                    "refused": [],
                }
        report = self._write_report(started, transitions, consolidation, dry_run)
        run = CuratorRun(started.isoformat(), transitions, consolidation, report, dry_run)
        if not dry_run:
            self._state.last_run_at = started.isoformat()
            self._state.run_count += 1
            self._state.last_summary = run.summary
            self._state.last_report = str(report) if report else None
            self._save_state()
        return run

    def _write_report(
        self,
        started: datetime,
        transitions: dict[str, int],
        consolidation: dict[str, Any],
        dry_run: bool,
    ) -> Path | None:
        directory = self.state_dir / REPORT_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = started.strftime("%Y%m%d-%H%M%S")
            path = directory / f"{stamp}{'-dry' if dry_run else ''}.md"
            lines = [f"# Curator run — {started.isoformat()}", ""]
            if dry_run:
                lines.append("_Dry run: nothing was changed._")
                lines.append("")
            lines.append("## Automatic transitions")
            lines.extend(f"- {k.replace('_', ' ')}: {v}" for k, v in transitions.items())
            lines.append("")
            lines.append("## Consolidation")
            if consolidation.get("skipped"):
                lines.append(f"- skipped: {consolidation['skipped']}")
            for item in consolidation.get("applied", []):
                lines.append(f"- applied: {item}")
            for item in consolidation.get("refused", []):
                lines.append(f"- refused: {item}")
            for item in consolidation.get("keep", []):
                lines.append(f"- kept: {item.get('name')} — {item.get('reason', '')}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return path
        except OSError:
            logger.debug("curator report write failed", exc_info=True)
            return None

    # -- user commands ------------------------------------------------------------

    def status_text(self) -> str:
        rows = self.candidates()
        mode = (
            "paused"
            if self._state.paused
            else ("enabled" if self.config.curator_enabled else "disabled")
        )
        cfg = self.config
        lines = [
            f"curator: {mode}",
            f"last run: {self._state.last_run_at or 'never'} ({self._state.run_count} runs)",
            f"interval: {cfg.curator_interval_hours:g}h; stale after "
            f"{cfg.curator_stale_after_days}d; archive after {cfg.curator_archive_after_days}d",
            f"consolidation: {'on' if cfg.curator_consolidate else 'off'}",
        ]
        if self._state.last_summary:
            lines.append(f"last summary: {self._state.last_summary}")
        lines.append(f"managed skills: {len(rows)}")
        for row in rows:
            flag = " (pinned)" if row["pinned"] else ""
            lines.append(
                f"  - {row['scope']}/{row['name']}: {row['state']}, uses={row['use_count']}{flag}"
            )
        archived = [
            f"{scope}/{n}"
            for scope in ("user", "project")
            for n in self.manager.usage[scope].archived_names()
        ]
        if archived:
            lines.append("archived: " + ", ".join(archived))
        return "\n".join(lines)

    def pin(self, scope: MemoryScope, name: str, pinned: bool) -> str:
        with self.manager.write_scope(scope):
            if self.manager.find(scope, name) is None:
                return f"skill {name!r} not found in the {scope} scope"
            self.manager.usage[scope].set_pinned(name, pinned)
        return f"{'pinned' if pinned else 'unpinned'} {scope}/{name}"

    def adopt(self, scope: MemoryScope, name: str) -> str:
        with self.manager.write_scope(scope):
            if self.manager.find(scope, name) is None:
                return f"skill {name!r} not found in the {scope} scope"
            self.manager.usage[scope].adopt(name)
        return f"{scope}/{name} is now curator-managed"

    def restore(self, scope: MemoryScope, name: str) -> str:
        with self.manager.write_scope(scope):
            ok, message = self.manager.usage[scope].restore(name)
            if ok:
                self.manager.ledger[scope].record(
                    "restore",
                    name,
                    actor="user",
                    before=[],
                    after_root=self.manager.roots.directory(scope) / name,
                )
        return str(message)


def _parse_plan(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        decoded = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    plan: dict[str, Any] = {}
    for key in ("patches", "creates", "support_files", "archives", "keep"):
        items = decoded.get(key, [])
        plan[key] = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    return plan


__all__ = [
    "CURATOR_SYSTEM_PROMPT",
    "REPORT_DIR",
    "STATE_FILE",
    "Curator",
    "CuratorRun",
    "CuratorState",
]
