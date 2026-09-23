"""Curator-owned durable state, consultation log and per-run reports.

Everything the Curator remembers lives under
``<paths.extension_state_dir>/curator/`` so a Skill root never holds Curator
bookkeeping:

```text
state.json      scheduler, pause flag, run history and per-Skill lifecycle state
usage.jsonl     append-only consultation log (``{skill,scope,consulted_at,source}``)
reports/<id>/   run.json (machine-readable) + REPORT.md (human-readable)
backups/<id>/   whole-library snapshots taken before a mutating pass
```

Every write is atomic (temp file in the destination directory, ``fsync``,
``os.replace``) and takes one advisory file lock, so a crashed Curator pass cannot
leave half a JSON document and two concurrent sessions cannot interleave appends.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None

STATE_FILE = "state.json"
USAGE_FILE = "usage.jsonl"
REPORTS_DIR = "reports"
BACKUPS_DIR = "backups"
LOCK_FILE = ".state.lock"

USAGE_SOURCES = ("slash_command", "skill_tool", "turn")
STATES = ("active", "stale", "archived")
DEFERRED_FIRST_RUN_SUMMARY = (
    "deferred first run - curator seeded, will run after one interval; "
    "use `/curator run --dry-run` to preview now"
)
_MAX_SUMMARY_CHARS = 500


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Render one datetime as an ISO-8601 UTC string."""
    return value.astimezone(UTC).isoformat()


def parse_epoch(value: Any) -> float | None:
    """Read a stored timestamp as epoch seconds, or ``None`` when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def epoch_to_iso(value: float | None) -> str | None:
    """Render epoch seconds as an ISO-8601 UTC string, or ``None``."""
    if value is None:
        return None
    return to_iso(datetime.fromtimestamp(value, UTC))


def run_id_for(now: datetime) -> str:
    """Return the report directory name for one pass."""
    return now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True, slots=True)
class ConsultationRecord:
    """One consultation of one Skill, as appended to ``usage.jsonl``."""

    skill: str
    scope: str
    consulted_at: str
    source: str

    def as_json(self) -> dict[str, str]:
        return {
            "skill": self.skill,
            "scope": self.scope,
            "consulted_at": self.consulted_at,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class RecordState:
    """The Curator lifecycle state of one Skill."""

    state: str = "active"
    since: str = ""

    def as_json(self) -> dict[str, str]:
        return {"state": self.state, "since": self.since}


@dataclass(slots=True)
class CuratorState:
    """The persisted Curator state document."""

    last_run_at: str | None = None
    last_run_duration_seconds: float | None = None
    last_run_summary: str | None = None
    last_run_summary_shown_at: str | None = None
    last_report_path: str | None = None
    paused: bool = False
    run_count: int = 0
    last_review_run_id: str | None = None
    last_review_run_at: str | None = None
    records: dict[str, RecordState] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "last_run_duration_seconds": self.last_run_duration_seconds,
            "last_run_summary": self.last_run_summary,
            "last_run_summary_shown_at": self.last_run_summary_shown_at,
            "last_report_path": self.last_report_path,
            "paused": self.paused,
            "run_count": self.run_count,
            "last_review_run_id": self.last_review_run_id,
            "last_review_run_at": self.last_review_run_at,
            "records": {key: value.as_json() for key, value in sorted(self.records.items())},
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> CuratorState:
        """Rebuild the state document, tolerating missing or wrong-typed fields."""
        state = cls()
        state.last_run_at = _text(raw.get("last_run_at"))
        state.last_run_duration_seconds = _number(raw.get("last_run_duration_seconds"))
        state.last_run_summary = _text(raw.get("last_run_summary"))
        state.last_run_summary_shown_at = _text(raw.get("last_run_summary_shown_at"))
        state.last_report_path = _text(raw.get("last_report_path"))
        state.paused = bool(raw.get("paused"))
        run_count = raw.get("run_count")
        state.run_count = run_count if isinstance(run_count, int) and run_count >= 0 else 0
        state.last_review_run_id = _text(raw.get("last_review_run_id"))
        state.last_review_run_at = _text(raw.get("last_review_run_at"))
        records = raw.get("records")
        if isinstance(records, Mapping):
            for key, value in records.items():
                if not isinstance(key, str) or not isinstance(value, Mapping):
                    continue
                name = _text(value.get("state"))
                if name not in STATES:
                    continue
                state.records[key] = RecordState(name, _text(value.get("since")) or "")
        return state

    def record(self, scope: str, name: str) -> RecordState:
        """Return the stored lifecycle state of one Skill."""
        return self.records.get(record_key(scope, name), RecordState())

    def set_record(self, scope: str, name: str, state: str, *, since: str) -> None:
        """Store one Skill's lifecycle state; an unknown state is refused."""
        if state not in STATES:
            raise ValueError(f"Unknown Curator state {state!r}")
        self.records[record_key(scope, name)] = RecordState(state, since)

    def records_in(self, state: str) -> dict[str, RecordState]:
        """Return every stored record currently in ``state``."""
        return {key: value for key, value in self.records.items() if value.state == state}


def record_key(scope: str, name: str) -> str:
    """Return the ``<scope>/<name>`` key used by ``state.json`` records."""
    return f"{scope}/{name}"


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write one file atomically, mirroring ``skill_manager._atomic_write``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold one cross-process advisory lock next to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="ascii")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            handle.seek(0)
            if handle.read(1) == "":
                handle.seek(0)
                handle.write("0")
                handle.flush()
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
        finally:
            handle.close()


class CuratorStateStore:
    """Paths, atomic writes and the consultation log for one Curator state root."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "curator"
        self.path = self.root / STATE_FILE
        self.usage_path = self.root / USAGE_FILE
        self.reports_dir = self.root / REPORTS_DIR
        self.backups_dir = self.root / BACKUPS_DIR
        self.lock_path = self.root / LOCK_FILE

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the state lock for a read-modify-write sequence."""
        with _locked(self.lock_path):
            yield

    # -- state.json ---------------------------------------------------------------

    def load(self) -> CuratorState:
        """Read the state document; a missing or damaged file reads as defaults."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CuratorState()
        if not isinstance(raw, dict):
            return CuratorState()
        return CuratorState.from_json(raw)

    def save(self, state: CuratorState) -> bool:
        """Persist the state document atomically; never raises."""
        try:
            text = json.dumps(state.as_json(), indent=2, sort_keys=True, ensure_ascii=False)
            with self.locked():
                _atomic_write_text(self.path, text + "\n")
            return True
        except (OSError, TypeError, ValueError):
            logger.warning("curator state save failed", exc_info=True)
            return False

    def update(self, **changes: object) -> CuratorState:
        """Apply field changes to the stored document under one lock."""
        with self.locked():
            state = self.load()
            for key, value in changes.items():
                if not hasattr(state, key):
                    raise ValueError(f"Unknown Curator state field {key!r}")
                setattr(state, key, value)
            text = json.dumps(state.as_json(), indent=2, sort_keys=True, ensure_ascii=False)
            _atomic_write_text(self.path, text + "\n")
        return state

    def set_paused(self, paused: bool) -> CuratorState:
        """Store the pause flag."""
        return self.update(paused=bool(paused))

    # -- usage.jsonl --------------------------------------------------------------

    def record_usage(
        self,
        *,
        skill: str,
        scope: str,
        source: str,
        consulted_at: datetime | None = None,
    ) -> bool:
        """Append one consultation line; an unknown source is refused."""
        name = skill.strip()
        if not name or source not in USAGE_SOURCES:
            return False
        record = ConsultationRecord(
            skill=name,
            scope=scope if scope in {"user", "project"} else "user",
            consulted_at=to_iso(consulted_at or utc_now()),
            source=source,
        )
        try:
            line = json.dumps(record.as_json(), ensure_ascii=False) + "\n"
            with self.locked():
                self.usage_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.usage_path, "a", encoding="utf-8") as stream:
                    stream.write(line)
            return True
        except OSError:
            logger.warning("curator usage append failed", exc_info=True)
            return False

    def usage(self) -> tuple[ConsultationRecord, ...]:
        """Read the whole consultation log; malformed lines are skipped."""
        try:
            lines = self.usage_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        records: list[ConsultationRecord] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("skill"), str):
                continue
            records.append(
                ConsultationRecord(
                    skill=str(raw["skill"]),
                    scope=str(raw.get("scope") or ""),
                    consulted_at=str(raw.get("consulted_at") or ""),
                    source=str(raw.get("source") or ""),
                )
            )
        return tuple(records)

    def last_consulted_at(
        self,
        skill: str,
        scope: str,
        *,
        not_before: float | None = None,
    ) -> float | None:
        """Return the newest consultation of one Skill inside the lookback window."""
        newest: float | None = None
        for record in self.usage():
            if record.skill != skill:
                continue
            if record.scope and scope and record.scope != scope:
                continue
            moment = parse_epoch(record.consulted_at)
            if moment is None or (not_before is not None and moment < not_before):
                continue
            if newest is None or moment > newest:
                newest = moment
        return newest

    # -- records ------------------------------------------------------------------

    def state_for(self, scope: str, name: str) -> RecordState:
        """Return the stored lifecycle state of one Skill."""
        return self.load().record(scope, name)

    def set_state(self, scope: str, name: str, state: str, *, since: datetime) -> None:
        """Store one Skill's lifecycle state in ``state.json``."""
        with self.locked():
            document = self.load()
            document.set_record(scope, name, state, since=to_iso(since))
            text = json.dumps(document.as_json(), indent=2, sort_keys=True, ensure_ascii=False)
            _atomic_write_text(self.path, text + "\n")

    # -- reports ------------------------------------------------------------------

    def report_dir(self, run_id: str) -> Path:
        """Return the report directory for one run id."""
        return self.reports_dir / run_id

    def next_run_id(self, now: datetime) -> str:
        """Return an unused report directory name for one pass."""
        base = run_id_for(now)
        candidate = base
        counter = 1
        while self.report_dir(candidate).exists():
            counter += 1
            candidate = f"{base}-{counter:02d}"
        return candidate

    def write_report(
        self,
        run_id: str,
        payload: Mapping[str, Any],
        markdown: str,
    ) -> Path | None:
        """Write ``run.json`` and ``REPORT.md`` for one pass; never raises."""
        directory = self.report_dir(run_id)
        try:
            with self.locked():
                directory.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(
                    directory / "run.json",
                    json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                )
                _atomic_write_text(directory / "REPORT.md", markdown)
            return directory
        except OSError:
            logger.warning("curator report write failed", exc_info=True)
            return None

    def read_report(self, run_id: str) -> str | None:
        """Read one human-readable report, or ``None`` when it is absent."""
        try:
            return (self.report_dir(run_id) / "REPORT.md").read_text(encoding="utf-8")
        except OSError:
            return None

    def read_run(self, run_id: str) -> dict[str, Any] | None:
        """Read one machine-readable run record, or ``None`` when it is absent."""
        try:
            raw = json.loads((self.report_dir(run_id) / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def report_ids(self) -> tuple[str, ...]:
        """Return every report id, newest first."""
        try:
            names = [item.name for item in self.reports_dir.iterdir() if item.is_dir()]
        except OSError:
            return ()
        return tuple(sorted(names, reverse=True))

    def latest_report_id(self, state: CuratorState | None = None) -> str | None:
        """Return the recorded report id, falling back to the newest directory."""
        document = state if state is not None else self.load()
        if document.last_report_path:
            name = Path(document.last_report_path).name
            if name:
                return name
        ids = self.report_ids()
        return ids[0] if ids else None


def due_for_run(state: CuratorState, *, now: datetime, interval_hours: float) -> bool:
    """Whether the cadence gate lets a background pass run now."""
    last = parse_epoch(state.last_run_at)
    if last is None:
        return False
    return (now.timestamp() - last) >= interval_hours * 3600.0


def summarize_transitions(counts: Mapping[str, int], *, dry_run: bool) -> str:
    """Render the one-line transition summary stored in ``state.json``."""
    parts = [f"{counts.get('marked_stale') or 0} marked stale"]
    parts.append(f"{counts.get('archived') or 0} archived")
    parts.append(f"{counts.get('reactivated') or 0} reactivated")
    prefix = "dry-run auto: " if dry_run else "auto: "
    return prefix + ", ".join(parts)


def clamp_summary(text: str) -> str:
    """Bound a one-line summary stored in ``state.json``."""
    flat = " ".join(text.split())
    if len(flat) <= _MAX_SUMMARY_CHARS:
        return flat
    return flat[: _MAX_SUMMARY_CHARS - 1] + "\u2026"


__all__ = [
    "BACKUPS_DIR",
    "DEFERRED_FIRST_RUN_SUMMARY",
    "LOCK_FILE",
    "REPORTS_DIR",
    "STATE_FILE",
    "STATES",
    "USAGE_FILE",
    "USAGE_SOURCES",
    "ConsultationRecord",
    "CuratorState",
    "CuratorStateStore",
    "RecordState",
    "clamp_summary",
    "due_for_run",
    "epoch_to_iso",
    "parse_epoch",
    "record_key",
    "run_id_for",
    "summarize_transitions",
    "to_iso",
    "utc_now",
]
