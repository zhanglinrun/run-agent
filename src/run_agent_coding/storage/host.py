"""Atomic extension bindings over the session host's existing database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from time import time

from run_agent_coding.host.contracts import (
    ArtifactRef,
    ContextSnapshot,
    ExtensionToken,
    HostPublication,
    HostServices,
    ScopedServices,
    ServiceScope,
    SessionActivation,
    SnapshotService,
    TaskHandler,
    TaskService,
)
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.resources import NamespaceResources
from run_agent_coding.storage.sessions import SqliteSessionRepository, canonical_json
from run_agent_coding.storage.snapshots import read_snapshot
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import ExtensionRetired, NamespaceState, assert_extension
from run_agent_coding.storage.tasks import BoundTaskService, LocalTaskManager
from run_agent_core.session.contracts import AppendReceipt


class SqliteHostServices:
    def __init__(self, database: SqliteDatabase, artifacts: ArtifactStore, owner_id: str) -> None:
        self.database, self.artifacts, self.owner_id = database, artifacts, owner_id
        self.tasks = LocalTaskManager(database)

    async def publish(
        self,
        session_id: str,
        generation: str,
        sources: Sequence[str],
        assert_active: Callable[[], None],
        *,
        expected_generation: str | None = None,
        handlers: Mapping[str, Mapping[str, TaskHandler]] | None = None,
        activation: SessionActivation | None = None,
    ) -> HostPublication:
        if (
            not generation
            or any(not source for source in sources)
            or len(set(sources)) != len(sources)
        ):
            raise ValueError("Invalid extension bindings")
        frozen_sources = tuple(sources)

        def publish(connection: sqlite3.Connection) -> tuple[str, str, AppendReceipt | None]:
            assert_active()
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if (
                session is None
                or session["owner_id"] != self.owner_id
                or not session["owner_active"]
                or session["owner_expires_at"] <= time()
            ):
                raise ExtensionRetired("Only the active host can publish extension services")
            owners = connection.execute(
                "SELECT generation FROM extension_owners "
                "WHERE session_id=? AND owner_id=? AND active=1",
                (session_id, self.owner_id),
            ).fetchall()
            if any(row[0] != expected_generation for row in owners):
                raise ExtensionRetired("The published extension generation changed")
            connection.execute(
                "UPDATE extension_owners SET active=0 WHERE session_id=?",
                (session_id,),
            )
            for source in frozen_sources:
                connection.execute(
                    "INSERT INTO extension_owners VALUES (?,?,?,?,1) "
                    "ON CONFLICT(session_id,source_id) DO UPDATE SET "
                    "owner_id=excluded.owner_id,generation=excluded.generation,active=1",
                    (session_id, source, self.owner_id, generation),
                )
            connection.execute(
                "UPDATE extension_tasks SET status='interrupted',finished_at=? "
                "WHERE session_id=? AND owner_id<>? "
                "AND status IN ('queued','running','cancelling')",
                (time(), session_id, self.owner_id),
            )
            receipt = None
            if activation is not None:
                if activation.token.session_id != session_id:
                    raise ExtensionRetired("Resource activation belongs to another session")
                receipt = SqliteSessionRepository(self.database).append_in_transaction(
                    connection,
                    (activation.entry,),
                    token=activation.token,
                    branch_id=activation.branch_id,
                    expected_head=activation.expected_head,
                )
            return session["principal_id"], session["project_id"], receipt

        principal_id, project_id, receipt = await self.database.run(publish, write=True)
        scopes: dict[ServiceScope, str] = {
            "session": canonical_json(["session", principal_id, session_id]),
            "project": canonical_json(["project", principal_id, project_id]),
            "user": canonical_json(["user", principal_id]),
        }
        services: dict[str, HostServices] = {
            source: BoundHostServices(
                self.database,
                self.artifacts,
                ExtensionToken(session_id, source, self.owner_id, generation),
                scopes,
                assert_active,
                self.tasks,
                dict((handlers or {}).get(source, {})),
            )
            for source in frozen_sources
        }
        return HostPublication(services, receipt)

    async def retire(self, session_id: str, generation: str) -> int:
        def retire(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE extension_owners SET active=0 "
                "WHERE session_id=? AND owner_id=? AND generation=?",
                (session_id, self.owner_id, generation),
            )

        error: Exception | None = None
        try:
            await self.database.run(retire, write=True)
        except Exception as exc:
            error = exc
        remaining = await self.tasks.retire(session_id, generation)
        if error is not None:
            raise error
        return remaining


class BoundHostServices:
    def __init__(
        self,
        database: SqliteDatabase,
        artifacts: ArtifactStore,
        token: ExtensionToken,
        scopes: Mapping[ServiceScope, str],
        assert_active: Callable[[], None],
        tasks: LocalTaskManager,
        handlers: dict[str, TaskHandler],
    ) -> None:
        self._assert_active = assert_active
        self._tasks = BoundTaskService(tasks, token, handlers, self, assert_active)
        self._snapshots = BoundSnapshots(database, token, assert_active)
        self._scopes: dict[ServiceScope, ScopedServices] = {}
        for name, scope in scopes.items():
            scoped_artifacts = ScopedArtifacts(database, artifacts, token, scope, assert_active)
            self._scopes[name] = ScopedServices(
                NamespaceState(database, token, scope, assert_active),
                NamespaceResources(
                    database,
                    token,
                    scope,
                    artifacts,
                    assert_active,
                    scoped_artifacts.read,
                ),
                scoped_artifacts,
            )

    def scope(self, scope: ServiceScope = "session") -> ScopedServices:
        self._assert_active()
        if scope not in self._scopes:
            raise ValueError("Scope must be session, project or user")
        return self._scopes[scope]

    @property
    def tasks(self) -> TaskService:
        self._assert_active()
        return self._tasks

    @property
    def snapshots(self) -> SnapshotService:
        self._assert_active()
        return self._snapshots


class BoundSnapshots:
    def __init__(
        self, database: SqliteDatabase, token: ExtensionToken, assert_active: Callable[[], None]
    ) -> None:
        self._database, self._token, self._assert_active = database, token, assert_active

    async def read(self, snapshot_id: str) -> ContextSnapshot:
        def read(connection: sqlite3.Connection) -> ContextSnapshot:
            self._assert_active()
            assert_extension(connection, self._token)
            value = read_snapshot(connection, snapshot_id, session_id=self._token.session_id)
            value.pop("created_at")
            return ContextSnapshot(**value)

        snapshot = await self._database.run(read)
        self._assert_active()
        return snapshot


class ScopedArtifacts:
    def __init__(
        self,
        database: SqliteDatabase,
        store: ArtifactStore,
        token: ExtensionToken,
        scope: str,
        assert_active: Callable[[], None],
    ) -> None:
        self._database, self._store, self._token = database, store, token
        self._owner_key = canonical_json([token.source_id, scope])
        self._assert_active = assert_active

    async def put(self, content: bytes) -> ArtifactRef:
        self._assert_active()
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("Extension artifacts are limited to 16 MiB")
        ref = await self._store.put(content)

        def register(connection: sqlite3.Connection) -> None:
            self._assert_active()
            assert_extension(connection, self._token)
            connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?)",
                (ref.digest, ref.size, time()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO artifact_refs VALUES ('extension',?,?)",
                (self._owner_key, ref.digest),
            )

        await self._database.run(register, write=True)
        return ref

    async def read(self, ref: ArtifactRef) -> bytes:
        def authorize(connection: sqlite3.Connection) -> None:
            self._assert_active()
            assert_extension(connection, self._token)
            row = connection.execute(
                "SELECT 1 FROM artifact_refs WHERE owner_kind='extension' AND owner_key=? "
                "AND digest=?",
                (self._owner_key, ref.digest),
            ).fetchone()
            if row is None:
                raise KeyError("Artifact does not belong to this extension scope")

        await self._database.run(authorize)
        result = await self._store.read(ref)
        self._assert_active()
        return result
