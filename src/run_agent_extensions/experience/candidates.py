"""Append-only Skill candidates and trusted project-file probes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from .scopes import Scope

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None

CANDIDATE_SCHEMA = "run-agent.skill-candidate.v1"
CANDIDATES_FILE = "candidates.jsonl"
BLOBS_DIR = "blobs"
MAX_OPERATIONS = 8
MAX_CHANGED_CHARS = 2_000
MAX_PROBE_BYTES = 1_048_576
CandidateStatus = Literal["cold", "verified", "published", "rejected", "superseded"]
OperationAction = Literal["add", "delete", "replace"]


class CandidateError(ValueError):
    """A candidate operation that failed closed."""


@dataclass(frozen=True, slots=True)
class CandidateOperation:
    action: OperationAction
    old_text: str = ""
    new_text: str = ""


@dataclass(frozen=True, slots=True)
class ProbeEvidence:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateClaim:
    text: str
    probes: tuple[ProbeEvidence, ...]


@dataclass(frozen=True, slots=True)
class StatusEvent:
    status: CandidateStatus
    timestamp: str
    reason: str = ""
    report_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    candidate_id: str
    scope: Scope
    name: str
    source_session: str
    source_run: str
    base_digest: str | None
    candidate_digest: str
    operations: tuple[CandidateOperation, ...]
    claims: tuple[CandidateClaim, ...]
    created_at: str
    status: CandidateStatus = "cold"
    report_id: str | None = None
    status_events: tuple[StatusEvent, ...] = ()


class ProjectProbe:
    """Read-only probes confined to one trusted project tree."""

    def __init__(self, project_root: Path, *, trusted: bool) -> None:
        self.project_root = project_root
        self.trusted = trusted

    def read(self, relative_path: str) -> bytes:
        target = self._resolve(relative_path)
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise CandidateError(f"probe file is not readable: {relative_path!r}") from exc
        if size > MAX_PROBE_BYTES:
            raise CandidateError(
                f"probe file {relative_path!r} is {size} bytes; limit is {MAX_PROBE_BYTES}"
            )
        try:
            return target.read_bytes()
        except OSError as exc:
            raise CandidateError(f"probe file is not readable: {relative_path!r}") from exc

    def digest(self, relative_path: str) -> ProbeEvidence:
        normalized = _relative_probe_path(relative_path)
        return ProbeEvidence(normalized, _sha256(self.read(normalized)))

    def grep(self, relative_path: str, pattern: str, *, limit: int = 100) -> tuple[str, ...]:
        """Return bounded matching lines without exposing an arbitrary filesystem reader."""
        if not pattern:
            raise CandidateError("probe grep pattern must not be empty")
        try:
            expression = re.compile(pattern)
        except re.error as exc:
            raise CandidateError(f"invalid probe grep pattern: {exc}") from exc
        try:
            text = self.read(relative_path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateError(f"probe file is not UTF-8 text: {relative_path!r}") from exc
        matches = [
            f"{number}:{line}"
            for number, line in enumerate(text.splitlines(), start=1)
            if expression.search(line)
        ]
        return tuple(matches[: max(0, limit)])

    def verify(self, evidence: ProbeEvidence) -> bool:
        try:
            return self.digest(evidence.path).sha256 == evidence.sha256
        except CandidateError:
            return False

    def _resolve(self, relative_path: str) -> Path:
        if not self.trusted:
            raise CandidateError("project probes require a trusted project")
        normalized = _relative_probe_path(relative_path)
        root = self.project_root.absolute()
        target = root.joinpath(*Path(normalized).parts).absolute()
        current = target
        while True:
            if _is_redirect(current):
                raise CandidateError(
                    f"probe path contains a symlink or junction: {relative_path!r}"
                )
            if current == root:
                break
            if current.parent == current:
                raise CandidateError(f"probe path escapes the project: {relative_path!r}")
            current = current.parent
        try:
            resolved_root = root.resolve(strict=True)
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise CandidateError(f"probe file does not exist: {relative_path!r}") from exc
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise CandidateError(f"probe path is not a project file: {relative_path!r}")
        return target


class SkillCandidateStore:
    """An append-only candidate log with content-addressed materialized bodies."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / CANDIDATES_FILE
        self.blobs = root / BLOBS_DIR
        self.lock_path = root / ".write.lock"

    def create(
        self,
        *,
        scope: Scope,
        name: str,
        source_session: str,
        source_run: str,
        base_content: str | None,
        operations: Sequence[CandidateOperation],
        claims: Sequence[CandidateClaim] = (),
        candidate_content: str | None = None,
    ) -> SkillCandidate:
        if not source_session.strip() or not source_run.strip():
            raise CandidateError("candidate source session and run are required")
        frozen_operations = tuple(operations)
        materialized = materialize_operations(base_content or "", frozen_operations)
        if candidate_content is not None and materialized != candidate_content:
            raise CandidateError("operations do not materialize the supplied candidate content")
        digest = _sha256(materialized.encode("utf-8"))
        base_digest = _sha256(base_content.encode("utf-8")) if base_content is not None else None
        created_at = _now()
        candidate = SkillCandidate(
            candidate_id=uuid4().hex,
            scope=scope,
            name=name,
            source_session=source_session,
            source_run=source_run,
            base_digest=base_digest,
            candidate_digest=digest,
            operations=frozen_operations,
            claims=tuple(claims),
            created_at=created_at,
            status_events=(StatusEvent("cold", created_at, "candidate proposed"),),
        )
        with self._locked():
            if self.get(candidate.candidate_id) is not None:  # pragma: no cover - UUID collision
                raise CandidateError("candidate id collision")
            self._store_blob(digest, materialized)
            self._append(_candidate_record(candidate))
        return candidate

    propose = create

    def content(self, candidate: SkillCandidate | str) -> str:
        current = self.require(candidate if isinstance(candidate, str) else candidate.candidate_id)
        path = self.blobs / f"{current.candidate_digest}.md"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CandidateError(f"candidate blob is missing: {current.candidate_digest}") from exc
        if _sha256(data) != current.candidate_digest:
            raise CandidateError(f"candidate blob digest mismatch: {current.candidate_id}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateError(f"candidate blob is not UTF-8: {current.candidate_id}") from exc

    def list(self, *, status: CandidateStatus | None = None) -> list[SkillCandidate]:
        candidates = list(self._load().values())
        if status is not None:
            candidates = [candidate for candidate in candidates if candidate.status == status]
        return sorted(candidates, key=lambda candidate: candidate.created_at, reverse=True)

    def get(self, candidate_id: str) -> SkillCandidate | None:
        return self._load().get(candidate_id)

    def require(self, candidate_id: str) -> SkillCandidate:
        candidate = self.get(candidate_id)
        if candidate is None:
            raise CandidateError(f"unknown candidate {candidate_id!r}")
        return candidate

    def transition(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        reason: str = "",
        report_id: str | None = None,
    ) -> SkillCandidate:
        with self._locked():
            candidate = self.require(candidate_id)
            allowed: dict[CandidateStatus, frozenset[CandidateStatus]] = {
                "cold": frozenset({"cold", "verified", "rejected", "superseded"}),
                "verified": frozenset({"verified", "published", "rejected", "superseded"}),
                "published": frozenset({"published"}),
                "rejected": frozenset({"rejected"}),
                "superseded": frozenset({"superseded"}),
            }
            if status not in allowed[candidate.status]:
                raise CandidateError(
                    f"invalid candidate transition {candidate.status!r} -> {status!r}"
                )
            if (
                candidate.status == status
                and (report_id is None or report_id == candidate.report_id)
                and not reason
            ):
                return candidate
            event = StatusEvent(status, _now(), reason, report_id or candidate.report_id)
            self._append(_event_record(candidate_id, event))
        return self.require(candidate_id)

    def supersede_others(self, published: SkillCandidate) -> None:
        for candidate in self.list():
            if (
                candidate.candidate_id != published.candidate_id
                and candidate.scope == published.scope
                and candidate.name == published.name
                and candidate.status in {"cold", "verified"}
            ):
                self.transition(
                    candidate.candidate_id,
                    "superseded",
                    reason=f"superseded by {published.candidate_id}",
                )

    def _load(self) -> dict[str, SkillCandidate]:
        if not self.path.is_file():
            return {}
        candidates: dict[str, SkillCandidate] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or raw.get("schema") != CANDIDATE_SCHEMA:
                continue
            kind = raw.get("type")
            candidate_id = str(raw.get("candidate_id") or "")
            if kind == "candidate":
                try:
                    candidate = _parse_candidate(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                candidates[candidate.candidate_id] = candidate
            elif kind == "status" and candidate_id in candidates:
                try:
                    event = _parse_event(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                current = candidates[candidate_id]
                candidates[candidate_id] = replace(
                    current,
                    status=event.status,
                    report_id=event.report_id or current.report_id,
                    status_events=(*current.status_events, event),
                )
        return candidates

    def _store_blob(self, digest: str, content: str) -> None:
        destination = self.blobs / f"{digest}.md"
        data = content.encode("utf-8")
        if destination.is_file():
            if destination.read_bytes() != data:
                raise CandidateError(f"candidate blob collision for {digest}")
            return
        self.blobs.mkdir(parents=True, exist_ok=True)
        temporary = self.blobs / f".tmp-{uuid4().hex}-{digest}.md"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _append(self, record: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="ascii")
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


def materialize_operations(base: str, operations: Sequence[CandidateOperation]) -> str:
    if not operations:
        raise CandidateError("a candidate needs at least one operation")
    if len(operations) > MAX_OPERATIONS:
        raise CandidateError(f"a candidate may contain at most {MAX_OPERATIONS} operations")
    changed = sum(
        len(operation.new_text)
        if operation.action == "add"
        else len(operation.old_text)
        if operation.action == "delete"
        else len(operation.old_text) + len(operation.new_text)
        for operation in operations
    )
    if changed > MAX_CHANGED_CHARS:
        raise CandidateError(
            f"candidate changes total {changed} characters; limit is {MAX_CHANGED_CHARS}"
        )
    current = base
    for index, operation in enumerate(operations, start=1):
        if operation.action == "add":
            if not operation.new_text:
                raise CandidateError(f"operation {index}: add needs new_text")
            if operation.old_text:
                current = _replace_unique(
                    current,
                    operation.old_text,
                    operation.old_text + operation.new_text,
                    index,
                )
            else:
                current += operation.new_text
        elif operation.action == "delete":
            if not operation.old_text or operation.new_text:
                raise CandidateError(
                    f"operation {index}: delete needs old_text and an empty new_text"
                )
            current = _replace_unique(current, operation.old_text, "", index)
        elif operation.action == "replace":
            if not operation.old_text:
                raise CandidateError(f"operation {index}: replace needs old_text")
            if operation.old_text == operation.new_text:
                raise CandidateError(f"operation {index}: replacement is unchanged")
            current = _replace_unique(current, operation.old_text, operation.new_text, index)
        else:  # pragma: no cover - protected by typed/Pydantic callers
            raise CandidateError(f"operation {index}: unknown action {operation.action!r}")
    if current == base:
        raise CandidateError("candidate operations produce no change")
    return current


def capture_claims(
    probe: ProjectProbe, claims: Iterable[tuple[str, Sequence[str]]]
) -> tuple[CandidateClaim, ...]:
    captured: list[CandidateClaim] = []
    for text, paths in claims:
        claim = text.strip()
        if not claim:
            raise CandidateError("claim text must not be empty")
        if not paths:
            raise CandidateError("each project claim needs at least one probe file")
        captured.append(CandidateClaim(claim, tuple(probe.digest(path) for path in paths)))
    return tuple(captured)


def _replace_unique(text: str, old: str, new: str, index: int) -> str:
    count = text.count(old)
    if count != 1:
        raise CandidateError(
            f"operation {index}: old_text must match exactly once (matched {count})"
        )
    return text.replace(old, new, 1)


def _relative_probe_path(value: str) -> str:
    text = value.replace("\\", "/").strip()
    path = Path(text)
    if not text or path.is_absolute() or path.drive or ".." in path.parts:
        raise CandidateError("probe path must be relative to the trusted project")
    if any(part in {"", "."} for part in path.parts):
        raise CandidateError("probe path must name a project file")
    return "/".join(path.parts)


def _is_redirect(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _candidate_record(candidate: SkillCandidate) -> dict[str, Any]:
    return {
        "schema": CANDIDATE_SCHEMA,
        "type": "candidate",
        "candidate_id": candidate.candidate_id,
        "scope": candidate.scope,
        "name": candidate.name,
        "source": {"session": candidate.source_session, "run": candidate.source_run},
        "base_digest": candidate.base_digest,
        "candidate_digest": candidate.candidate_digest,
        "content_blob": f"{BLOBS_DIR}/{candidate.candidate_digest}.md",
        "operations": [
            {
                "action": operation.action,
                "old_text": operation.old_text,
                "new_text": operation.new_text,
            }
            for operation in candidate.operations
        ],
        "claims": [
            {
                "text": claim.text,
                "probes": [
                    {"path": evidence.path, "sha256": evidence.sha256} for evidence in claim.probes
                ],
            }
            for claim in candidate.claims
        ],
        "created_at": candidate.created_at,
        "status": "cold",
        "events": [
            {
                "status": event.status,
                "timestamp": event.timestamp,
                "reason": event.reason,
                "report_id": event.report_id,
            }
            for event in candidate.status_events
        ],
    }


def _event_record(candidate_id: str, event: StatusEvent) -> dict[str, Any]:
    return {
        "schema": CANDIDATE_SCHEMA,
        "type": "status",
        "candidate_id": candidate_id,
        "status": event.status,
        "timestamp": event.timestamp,
        "reason": event.reason,
        "report_id": event.report_id,
    }


def _parse_candidate(raw: Mapping[str, Any]) -> SkillCandidate:
    scope = str(raw["scope"])
    if scope not in {"user", "project"}:
        raise ValueError("invalid scope")
    source = cast(Mapping[str, Any], raw["source"])
    operations = tuple(
        CandidateOperation(
            cast(OperationAction, item["action"]),
            str(item.get("old_text") or ""),
            str(item.get("new_text") or ""),
        )
        for item in cast(list[Mapping[str, Any]], raw.get("operations") or [])
    )
    claims = tuple(
        CandidateClaim(
            str(item["text"]),
            tuple(
                ProbeEvidence(str(probe["path"]), str(probe["sha256"]))
                for probe in cast(list[Mapping[str, Any]], item.get("probes") or [])
            ),
        )
        for item in cast(list[Mapping[str, Any]], raw.get("claims") or [])
    )
    events = tuple(
        StatusEvent(
            cast(CandidateStatus, item["status"]),
            str(item["timestamp"]),
            str(item.get("reason") or ""),
            str(item["report_id"]) if item.get("report_id") else None,
        )
        for item in cast(list[Mapping[str, Any]], raw.get("events") or [])
    )
    status = events[-1].status if events else cast(CandidateStatus, raw.get("status") or "cold")
    report_id = next((event.report_id for event in reversed(events) if event.report_id), None)
    return SkillCandidate(
        candidate_id=str(raw["candidate_id"]),
        scope=cast(Scope, scope),
        name=str(raw["name"]),
        source_session=str(source["session"]),
        source_run=str(source["run"]),
        base_digest=str(raw["base_digest"]) if raw.get("base_digest") else None,
        candidate_digest=str(raw["candidate_digest"]),
        operations=operations,
        claims=claims,
        created_at=str(raw["created_at"]),
        status=status,
        report_id=report_id,
        status_events=events,
    )


def _parse_event(raw: Mapping[str, Any]) -> StatusEvent:
    status = str(raw["status"])
    if status not in {"cold", "verified", "published", "rejected", "superseded"}:
        raise ValueError("invalid status")
    return StatusEvent(
        cast(CandidateStatus, status),
        str(raw["timestamp"]),
        str(raw.get("reason") or ""),
        str(raw["report_id"]) if raw.get("report_id") else None,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "BLOBS_DIR",
    "CANDIDATES_FILE",
    "CANDIDATE_SCHEMA",
    "MAX_CHANGED_CHARS",
    "MAX_OPERATIONS",
    "CandidateClaim",
    "CandidateError",
    "CandidateOperation",
    "CandidateStatus",
    "ProbeEvidence",
    "ProjectProbe",
    "SkillCandidate",
    "SkillCandidateStore",
    "StatusEvent",
    "capture_claims",
    "materialize_operations",
]
