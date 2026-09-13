"""An append-only audit ledger of every skill mutation, with single-edit rollback.

Every change to a skill directory, whoever made it, appends one JSONL entry to
``<skills-dir>/.ledger.jsonl`` naming the actor (``agent``, ``review``, ``curator`` or
``user``), the action, and before/after file manifests. File contents are stored
content-addressed under ``<skills-dir>/.blobs/<sha256>``, so identical content across
entries is stored once and a mutation that touches one file costs one small blob.

The ledger is telemetry, not a gate: a ledger failure must never block the mutation it
describes, so every write path swallows its own errors. The one exception is
``rollback``, which fails closed: it refuses when a needed blob is missing, and it
captures the current state of every touched path as a safety entry before restoring,
so a rollback is itself undoable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

LEDGER_FILE = ".ledger.jsonl"
BLOBS_DIR = ".blobs"
VALID_ACTORS = frozenset({"agent", "review", "curator", "user"})


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: str
    timestamp: str
    actor: str
    action: str
    skill: str
    evidence: dict[str, Any]
    before: tuple[dict[str, str], ...]
    after: tuple[dict[str, str], ...]


class SkillLedger:
    def __init__(self, skills_dir: Path, *, enabled: bool = True) -> None:
        self.skills_dir = skills_dir
        self.path = skills_dir / LEDGER_FILE
        self.blobs = skills_dir / BLOBS_DIR
        self.enabled = enabled

    # -- blobs --------------------------------------------------------------------

    def _store_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        destination = self.blobs / digest
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".tmp-{uuid4().hex[:8]}-{digest}")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        return digest

    def read_blob(self, digest: str) -> bytes | None:
        if not digest or any(c not in "0123456789abcdef" for c in digest):
            return None
        path = self.blobs / digest
        try:
            return path.read_bytes() if path.exists() else None
        except OSError:
            return None

    def snapshot(self, root: Path | None) -> list[dict[str, str]]:
        """Manifest of every file under ``root`` with its content stored as a blob."""
        if root is None or not root.is_dir():
            return []
        manifest: list[dict[str, str]] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            try:
                manifest.append({"path": str(path), "sha256": self._store_blob(path.read_bytes())})
            except OSError:
                continue
        return manifest

    # -- append -------------------------------------------------------------------

    def capture_before(self, root: Path | None) -> list[dict[str, str]] | None:
        if not self.enabled:
            return None
        try:
            return self.snapshot(root)
        except Exception:
            logger.warning("ledger before-capture failed; mutation unaffected", exc_info=True)
            return None

    def append(
        self,
        action: str,
        skill: str,
        *,
        actor: str,
        before: list[dict[str, str]] | None = None,
        after: list[dict[str, str]] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        try:
            entry = {
                "id": uuid4().hex[:12],
                "ts": datetime.now(UTC).isoformat(),
                "actor": actor if actor in VALID_ACTORS else "agent",
                "action": action,
                "skill": skill,
                "evidence": evidence or {},
                "before": before or [],
                "after": after or [],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return str(entry["id"])
        except Exception:
            logger.warning("ledger append failed; mutation unaffected", exc_info=True)
            return None

    def record(
        self,
        action: str,
        skill: str,
        *,
        actor: str,
        before: list[dict[str, str]] | None,
        after_root: Path | None,
        evidence: dict[str, Any] | None = None,
    ) -> str | None:
        """Capture the after-state and append; never raises, never blocks."""
        if not self.enabled:
            return None
        try:
            after = self.snapshot(after_root)
            return self.append(
                action, skill, actor=actor, before=before or [], after=after, evidence=evidence
            )
        except Exception:
            logger.warning("ledger record failed; mutation unaffected", exc_info=True)
            return None

    # -- read ---------------------------------------------------------------------

    def entries(self, *, skill: str | None = None, limit: int | None = None) -> list[LedgerEntry]:
        """Newest first; malformed lines are skipped."""
        if not self.path.exists():
            return []
        rows: list[LedgerEntry] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                rows.append(
                    LedgerEntry(
                        str(raw.get("id", "")),
                        str(raw.get("ts", "")),
                        str(raw.get("actor", "")),
                        str(raw.get("action", "")),
                        str(raw.get("skill", "")),
                        dict(raw.get("evidence") or {}),
                        tuple(dict(i) for i in raw.get("before") or [] if isinstance(i, dict)),
                        tuple(dict(i) for i in raw.get("after") or [] if isinstance(i, dict)),
                    )
                )
        except OSError:
            return []
        if skill:
            rows = [row for row in rows if row.skill == skill]
        rows.reverse()
        return rows[:limit] if limit is not None else rows

    def get(self, entry_id: str) -> LedgerEntry | None:
        return next((row for row in self.entries() if row.id == entry_id), None)

    # -- rollback -----------------------------------------------------------------

    def _within_root(self, path: Path) -> bool:
        try:
            root = Path(os.path.normpath(str(self.skills_dir)))
            candidate = Path(os.path.normpath(str(path)))
            return candidate == root or root in candidate.parents
        except Exception:
            return False

    def rollback(self, entry_id: str) -> tuple[bool, str]:
        """Restore the before-state of one mutation, failing closed on any doubt."""
        entry = self.get(entry_id)
        if entry is None:
            return False, f"no ledger entry with id {entry_id!r}"
        for item in (*entry.before, *entry.after):
            if not self._within_root(Path(item.get("path", ""))):
                return (
                    False,
                    f"refusing rollback: entry references a path outside {self.skills_dir}",
                )
        for item in entry.before:
            if self.read_blob(item.get("sha256", "")) is None:
                return False, (
                    f"missing blob {item.get('sha256')} for {item.get('path')}; rollback "
                    "aborted, nothing was changed"
                )
        touched = {i["path"] for i in (*entry.before, *entry.after) if i.get("path")}
        try:
            safety: list[dict[str, str]] = []
            for raw_path in sorted(touched):
                path = Path(raw_path)
                if path.is_file():
                    safety.append({"path": raw_path, "sha256": self._store_blob(path.read_bytes())})
            safety_id = self.append(
                "pre-rollback",
                entry.skill,
                actor="user",
                before=safety,
                after=safety,
                evidence={"rollback_target": entry_id},
            )
        except Exception as exc:
            return False, f"pre-rollback safety capture failed ({exc}); nothing was changed"
        if safety_id is None:
            return (
                False,
                "pre-rollback safety capture failed (ledger disabled); nothing was changed",
            )
        before_paths = {i["path"] for i in entry.before}
        restored = removed = 0
        for item in entry.before:
            data = self.read_blob(item["sha256"])
            assert data is not None
            path = Path(item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            restored += 1
        for item in entry.after:
            raw_path = item.get("path", "")
            if raw_path and raw_path not in before_paths:
                path = Path(raw_path)
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:
                    logger.warning("could not remove %s during rollback", raw_path)
        self.append(
            "rollback",
            entry.skill,
            actor="user",
            before=safety,
            after=list(entry.before),
            evidence={"rollback_target": entry_id, "restored": restored, "removed": removed},
        )
        return True, (
            f"rolled back {entry_id} ({entry.action} on {entry.skill!r}): {restored} file(s) "
            f"restored, {removed} removed; safety entry {safety_id} holds the prior state"
        )


__all__ = ["BLOBS_DIR", "LEDGER_FILE", "VALID_ACTORS", "LedgerEntry", "SkillLedger"]
