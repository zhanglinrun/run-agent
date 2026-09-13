"""Async session metadata and writer ownership over the shared SQLite database."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.handle import OutcomeCommitter, SqliteSessionHandle
from run_agent_coding.storage.host import SqliteHostServices
from run_agent_coding.storage.sessions import SessionRecord, SqliteSessionRepository
from run_agent_coding.storage.settle import settle
from run_agent_coding.storage.skill_packages import SkillPackageStore
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.telemetry import SqliteTelemetrySink
from run_agent_core.session.contracts import SessionConflict


def normalize_session_name(value: str) -> str:
    name = value.strip()
    if not name or any(char in name for char in "\r\n\t"):
        raise ValueError("Session name must be nonempty and on one line")
    return name


def validate_session_id(session_id: str) -> None:
    if (
        not session_id
        or len(session_id.encode("utf-8")) > 128
        or any(ord(char) < 32 for char in session_id)
    ):
        raise ValueError("Session identity must be nonempty and at most 128 UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class CodingSessionRecord:
    id: str
    cwd: Path
    model: str
    title: str | None
    created_at: float
    updated_at: float
    provider_name: str | None = None
    project_id: str = ""
    principal_id: str = "local"

    @classmethod
    def from_record(cls, record: SessionRecord) -> CodingSessionRecord:
        return cls(
            record.session_id,
            Path(record.cwd),
            record.model,
            record.title,
            record.created_at,
            record.updated_at,
            record.provider_name,
            record.project_id,
            record.principal_id,
        )


class SessionManager:
    def __init__(
        self,
        paths: RunAgentPaths | None = None,
        *,
        database: SqliteDatabase | None = None,
        principal_id: str = "local",
        owner_id: str | None = None,
    ) -> None:
        self.paths = paths or RunAgentPaths()
        self.principal_id = principal_id
        self.owner_id = owner_id or uuid4().hex
        self._database = database
        self._owns_database = database is None
        self._lock = asyncio.Lock()
        self._handle_lock = asyncio.Lock()
        self._handles: dict[str, SqliteSessionHandle] = {}
        self._closed = False
        self._telemetry: SqliteTelemetrySink | None = None
        self._services: SqliteHostServices | None = None
        self._skill_packages: SkillPackageStore | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def repository(self) -> SqliteSessionRepository:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Session manager is closed")
            if self._database is None:
                self._database = await SqliteDatabase.open(self.paths.database_path)
            return SqliteSessionRepository(self._database)

    async def host_services(self) -> SqliteHostServices:
        repository = await self.repository()
        if self._services is None:
            self._services = SqliteHostServices(
                repository.database,
                ArtifactStore(self.paths.home / "artifacts"),
                self.owner_id,
            )
        return self._services

    async def skill_packages(self) -> SkillPackageStore:
        repository = await self.repository()
        if self._skill_packages is None:
            self._skill_packages = SkillPackageStore(
                repository.database,
                ArtifactStore(self.paths.home / "artifacts"),
                self.paths.home / "cache" / "skills",
            )
        return self._skill_packages

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
        if session_id is not None:
            validate_session_id(session_id)
        repository = await self.repository()
        record = await repository.create_session(
            cwd=cwd,
            model=model,
            principal_id=self.principal_id,
            session_id=session_id,
            project_id=project_id,
            provider_name=provider_name,
            title=title,
            metadata={},
        )
        return CodingSessionRecord.from_record(record)

    async def telemetry(self) -> SqliteTelemetrySink:
        repository = await self.repository()
        if self._telemetry is None:
            self._telemetry = SqliteTelemetrySink(repository.database)
        return self._telemetry

    async def get_session(self, session_id: str) -> CodingSessionRecord | None:
        repository = await self.repository()
        try:
            record = await repository.get_session(session_id)
        except KeyError:
            return None
        if record.principal_id != self.principal_id:
            return None
        return CodingSessionRecord.from_record(record)

    async def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        repository = await self.repository()
        records = await repository.list_sessions(principal_id=self.principal_id, limit=1000)
        canonical = str(cwd.resolve()) if cwd is not None else None
        return [
            CodingSessionRecord.from_record(record)
            for record in records
            if canonical is None or record.cwd == canonical
        ]

    async def open_storage(
        self, session_id: str, *, committer: OutcomeCommitter | None = None
    ) -> SqliteSessionHandle:
        async with self._handle_lock:
            if self._closed:
                raise RuntimeError("Session manager is closed")
            existing = self._handles.get(session_id)
            if existing is not None and not existing.closed:
                raise SessionConflict("Session already has an open writer")
            handle, cancelled = await settle(self._open_storage(session_id, committer=committer))
            if cancelled:
                await settle(handle.aclose())
                raise asyncio.CancelledError
            return handle

    async def _open_storage(
        self, session_id: str, *, committer: OutcomeCommitter | None
    ) -> SqliteSessionHandle:
        record = await self.get_session(session_id)
        if record is None:
            raise ValueError(f"Unknown session: {session_id}")
        repository = await self.repository()
        token = await repository.claim(
            session_id, owner_id=self.owner_id, run_id=f"initial-{uuid4().hex}", ttl_seconds=120
        )
        try:
            branch_id = await repository.database.run(
                lambda connection: connection.execute(
                    "SELECT active_branch_id FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0]
            )
        except BaseException:
            await settle(repository.release(token))
            raise
        handle = SqliteSessionHandle(repository, token, branch_id, committer=committer)
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
        handle = self._handles.get(session_id)
        if handle is None:
            raise SessionConflict("Metadata updates require an owned session writer")
        repository = await self.repository()
        record = await repository.get_session(session_id)
        metadata = dict(record.metadata)
        updated = await repository.update_metadata(
            handle.token,
            model=model or record.model,
            provider_name=provider_name or record.provider_name,
            title=title,
            metadata=metadata,
        )
        return CodingSessionRecord.from_record(updated)

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
            try:
                if self._telemetry is not None:
                    await self._telemetry.aclose()
            finally:
                if self._owns_database and self._database is not None:
                    await self._database.aclose()
