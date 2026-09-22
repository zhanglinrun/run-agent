"""Create, index, list, and resume JSONL coding sessions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from uuid import uuid4

from run_agent_coding.host.evaluation import EvaluationService
from run_agent_coding.jsonl_storage import SessionWriter
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.storage.host import MemoryHostServices
from run_agent_coding.storage.settle import settle
from run_agent_coding.storage.skill_packages import SkillPackageStore
from run_agent_coding.storage.telemetry import JsonlTelemetrySink
from run_agent_core.session.contracts import SessionConflict
from run_agent_core.session.entries import LeafEntry, SessionEntry
from run_agent_core.session.storage import (
    JsonlSessionStorage,
    _fsync_directory,
    _lock_file,
    _unlock_file,
)

_MAX_SESSION_ID_BYTES = 128
_RESERVED_SESSION_IDS = frozenset({"default", "index"})
_WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_INDEX_COMPACTION_MIN_RECORDS = 1024


def normalize_session_name(value: str) -> str:
    name = value.strip()
    if not name or any(char in name for char in "\r\n\t"):
        raise ValueError("Session name must be nonempty and on one line")
    return name


def validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "Session id must be non-empty, contain only alphanumeric characters, '-', '_', "
            "and '.', and start and end with an alphanumeric character"
        )
    if len(session_id.encode("utf-8")) > _MAX_SESSION_ID_BYTES:
        raise ValueError("Session identity must be nonempty and at most 128 UTF-8 bytes")
    normalized_id = session_id.casefold()
    if normalized_id in _RESERVED_SESSION_IDS:
        raise ValueError(f"Session id is reserved: {session_id}")
    if normalized_id.partition(".")[0] in _WINDOWS_RESERVED_FILE_STEMS:
        raise ValueError(f"Session id is not a portable file name: {session_id}")


@dataclass(frozen=True, slots=True)
class CodingSessionRecord:
    id: str
    cwd: Path
    model: str
    title: str | None
    created_at: float
    updated_at: float
    provider_name: str | None = None
    path: Path | None = None
    project_id: str = ""
    principal_id: str = "local"

    def to_json(self) -> dict[str, object]:
        path = self.path if self.path is not None else Path(f"{self.id}.jsonl")
        return {
            "id": self.id,
            "path": str(path),
            "cwd": str(self.cwd),
            "model": self.model,
            "provider_name": self.provider_name,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> CodingSessionRecord:
        return cls(
            id=str(payload["id"]),
            cwd=Path(str(payload["cwd"])),
            model=str(payload.get("model") or ""),
            title=None if payload.get("title") is None else str(payload["title"]),
            created_at=float(str(payload.get("created_at") or 0)),
            updated_at=float(str(payload.get("updated_at") or 0)),
            provider_name=(
                None if payload.get("provider_name") is None else str(payload["provider_name"])
            ),
            path=Path(str(payload["path"])) if payload.get("path") else None,
        )


class SessionManager:
    def __init__(
        self,
        paths: RunAgentPaths | None = None,
        *,
        principal_id: str = "local",
        owner_id: str | None = None,
        evaluation: EvaluationService | None = None,
    ) -> None:
        self.paths = paths or RunAgentPaths()
        self.principal_id = principal_id
        self.owner_id = owner_id or uuid4().hex
        self._evaluation = evaluation
        self._lock = asyncio.Lock()
        self._handle_lock = asyncio.Lock()
        self._handles: dict[str, SessionWriter] = {}
        self._closed = False
        self._telemetry: JsonlTelemetrySink | None = None
        self._services: MemoryHostServices | None = None
        self._skill_packages: SkillPackageStore | None = None
        self._close_task: asyncio.Task[None] | None = None

    def project_index_path(self, cwd: Path) -> Path:
        return self.paths.project_session_dir(cwd) / "index.jsonl"

    def _catalog_path(self) -> Path:
        return self.paths.sessions_dir / "index.jsonl"

    async def host_services(self) -> MemoryHostServices:
        if self._services is None:
            self._services = MemoryHostServices(
                self.owner_id,
                isolation_key=str(self.paths.home.resolve()),
                principal_id=self.principal_id,
                evaluation=self._evaluation,
            )
        return self._services

    async def skill_packages(self) -> SkillPackageStore:
        if self._skill_packages is None:
            self._skill_packages = SkillPackageStore(self.paths.home / "cache" / "skills")
        return self._skill_packages

    async def telemetry(self) -> JsonlTelemetrySink:
        if self._telemetry is None:
            self._telemetry = JsonlTelemetrySink(self.paths.logs_dir / "observations.jsonl")
        return self._telemetry

    async def create_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> CodingSessionRecord:
        del project_id
        if session_id is not None:
            validate_session_id(session_id)
        record = self._prepare_session(
            cwd=cwd, model=model, provider_name=provider_name, title=title, session_id=session_id
        )
        assert record.path is not None
        record.path.parent.mkdir(parents=True, exist_ok=True)
        if record.path.exists() and record.path.stat().st_size:
            raise RuntimeError(f"Session already exists with id '{record.id}'")
        record.path.touch()
        self._upsert(record)
        return record

    def _prepare_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None,
        title: str | None,
        session_id: str | None,
    ) -> CodingSessionRecord:
        now = time()
        resolved_cwd = cwd.resolve()
        record_id = uuid4().hex if session_id is None else session_id
        validate_session_id(record_id)
        path = self.paths.project_session_dir(resolved_cwd) / f"{record_id}.jsonl"
        return CodingSessionRecord(
            id=record_id,
            cwd=resolved_cwd,
            model=model,
            title=title,
            created_at=now,
            updated_at=now,
            provider_name=provider_name,
            path=path,
        )

    async def get_session(self, session_id: str) -> CodingSessionRecord | None:
        for record in self._read_all_records():
            if record.id == session_id:
                return record
        return None

    async def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        records = self._read_project_records(cwd) if cwd is not None else self._read_all_records()
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    async def open_storage(
        self, session_id: str, *, committer: object | None = None
    ) -> SessionWriter:
        del committer
        async with self._handle_lock:
            if self._closed:
                raise RuntimeError("Session manager is closed")
            existing = self._handles.get(session_id)
            if existing is not None and not existing.closed:
                raise SessionConflict("Session already has an open writer")
            handle, cancelled = await settle(self._open_storage(session_id))
            if cancelled:
                await settle(handle.aclose())
                raise asyncio.CancelledError
            return handle

    async def _open_storage(self, session_id: str) -> SessionWriter:
        record = await self.get_session(session_id)
        if record is None:
            raise ValueError(f"Unknown session: {session_id}")
        path = record.path or self.paths.project_session_dir(record.cwd) / f"{record.id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = SessionWriter(JsonlSessionStorage(path), session_id, owner_id=self.owner_id)
        services = await self.host_services()
        services.attach_writer(handle, cwd=record.cwd)
        self._handles[session_id] = handle
        return handle

    async def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        provider_name: str | None = None,
        title: str | None = None,
    ) -> CodingSessionRecord:
        existing = await self.get_session(session_id)
        if existing is None:
            raise SessionConflict("Metadata updates require a known session")
        updated = CodingSessionRecord(
            id=existing.id,
            cwd=existing.cwd,
            model=model or existing.model,
            title=title if title is not None else existing.title,
            created_at=existing.created_at,
            updated_at=time(),
            provider_name=provider_name if provider_name is not None else existing.provider_name,
            path=existing.path,
            project_id=existing.project_id,
            principal_id=existing.principal_id,
        )
        self._upsert(updated)
        return updated

    async def fork_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None,
        title: str | None,
        entries: Sequence[SessionEntry],
        current_id: str,
    ) -> CodingSessionRecord:
        record = await self.create_session(
            cwd=cwd, model=model, provider_name=provider_name, title=title
        )
        assert record.path is not None
        copied = tuple(entries)
        if current_id is not None and (not copied or not isinstance(copied[-1], LeafEntry)):
            copied = (*copied, LeafEntry(parent_id=current_id, entry_id=current_id))
        await JsonlSessionStorage(record.path).append_batch(copied)
        return record

    def _read_index(self, path: Path) -> list[CodingSessionRecord]:
        with self._locked_index(path, exclusive=False):
            return list(_read_index_unlocked(path).values())

    def _read_project_records(self, cwd: Path) -> list[CodingSessionRecord]:
        resolved = cwd.resolve()
        return [
            record
            for record in self._read_index(self.project_index_path(resolved))
            if record.cwd == resolved
        ]

    def _read_all_records(self) -> list[CodingSessionRecord]:
        records = self._read_index(self._catalog_path())
        for index_path in self.paths.sessions_dir.rglob("index.jsonl"):
            records.extend(self._read_index(index_path))
        return _deduplicate_records(records)

    def rebuild_catalog(self, cwds: Iterable[Path]) -> list[CodingSessionRecord]:
        """Rebuild the derived catalog cache from the project indexes.

        The project indexes are the source of truth and the catalog is only a cache, so
        rebuilding it requires the caller to supply the working directories to replay.
        """
        for cwd in cwds:
            for record in self._read_project_records(cwd):
                self._write_index(self._catalog_path(), record)
        return self._read_index(self._catalog_path())

    def _upsert(self, record: CodingSessionRecord) -> None:
        self._write_index(self.project_index_path(record.cwd), record)
        self._write_index(self._catalog_path(), record)

    def _write_index(self, path: Path, record: CodingSessionRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record.to_json(), ensure_ascii=False) + "\n").encode("utf-8")
        with self._locked_index(path, exclusive=True):
            records, row_count = _read_index_state_unlocked(path)
            with path.open("ab") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            records[record.id] = record
            if row_count + 1 > max(_INDEX_COMPACTION_MIN_RECORDS, 4 * len(records)):
                self._compact_index_unlocked(path, records.values())

    @contextmanager
    def _locked_index(self, path: Path, *, exclusive: bool) -> Iterator[None]:
        lock_path = path.with_name(f".{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            os.chmod(lock_path, 0o600)
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _lock_file(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(lock_file)

    def _compact_index_unlocked(self, path: Path, records: Iterable[CodingSessionRecord]) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as file:
                for record in records:
                    line = json.dumps(record.to_json(), ensure_ascii=False) + "\n"
                    file.write(line.encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        except BaseException:
            with suppress(OSError):
                temporary_path.unlink()
            raise

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())

        async def wait_close() -> None:
            assert self._close_task is not None
            await self._close_task

        _, cancelled = await settle(wait_close())
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        async with self._handle_lock:
            self._closed = True
            handles = tuple(self._handles.values())
        try:
            task_error: BaseException | None = None
            if self._services is not None:
                try:
                    await self._services.tasks.aclose()
                except BaseException as exc:
                    task_error = exc
            outcomes = await asyncio.gather(
                *(handle.aclose() for handle in handles), return_exceptions=True
            )
            errors = [item for item in outcomes if isinstance(item, BaseException)]
            if task_error is not None:
                errors.insert(0, task_error)
            if errors:
                raise errors[0]
        finally:
            if self._telemetry is not None:
                await self._telemetry.aclose()


def _read_index_unlocked(path: Path) -> dict[str, CodingSessionRecord]:
    records, _ = _read_index_state_unlocked(path)
    return records


def _read_index_state_unlocked(path: Path) -> tuple[dict[str, CodingSessionRecord], int]:
    records: dict[str, CodingSessionRecord] = {}
    row_count = 0
    if not path.exists():
        return records, row_count
    for line in path.read_text(encoding="utf-8").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            record = CodingSessionRecord.from_json(payload)
            records[record.id] = record
            row_count += 1
    return records, row_count


def _deduplicate_records(records: list[CodingSessionRecord]) -> list[CodingSessionRecord]:
    by_id: dict[str, CodingSessionRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None or record.updated_at >= existing.updated_at:
            by_id[record.id] = record
    return list(by_id.values())
