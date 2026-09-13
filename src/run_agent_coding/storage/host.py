"""Atomic extension bindings over the session host's existing database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from time import time

from run_agent_coding.host.context_resources import ResourceView
from run_agent_coding.host.contracts import (
    ArtifactRef,
    ContextSnapshot,
    ExtensionToken,
    HistoryService,
    HostPublication,
    HostServices,
    ResourceVersion,
    ScopedServices,
    ServiceScope,
    SessionActivation,
    SnapshotService,
    TaskHandler,
    TaskService,
)
from run_agent_coding.host.evaluation import (
    EvaluationService,
    UnavailableEvaluation,
)
from run_agent_coding.host.inference import InferenceService, UnavailableInference
from run_agent_coding.host.maintenance import MaintenanceRegistry
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.resources import NamespaceResources
from run_agent_coding.storage.sessions import SqliteSessionRepository, canonical_json, decode_entry
from run_agent_coding.storage.snapshots import read_snapshot
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import ExtensionRetired, NamespaceState, assert_extension
from run_agent_coding.storage.tasks import BoundTaskService, LocalTaskManager
from run_agent_coding.storage.unit_of_work import CommitParticipant, UnitOfWork
from run_agent_core.session.contracts import AppendReceipt
from run_agent_core.session.entries import CustomEntry, SessionEntry


class SqliteHostServices:
    def __init__(
        self,
        database: SqliteDatabase,
        artifacts: ArtifactStore,
        owner_id: str,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.database, self.artifacts, self.owner_id = database, artifacts, owner_id
        self.fault = fault
        self.tasks = LocalTaskManager(database)
        self.maintenance = MaintenanceRegistry()

    def _hit_fault(self, point: str) -> None:
        """Fire a named fault point; raising here aborts the enclosing transaction."""
        if self.fault is not None:
            self.fault(point)

    async def capture_resources(
        self, session_id: str, sources: Sequence[str], assert_active: Callable[[], None]
    ) -> Mapping[str, ResourceView]:
        frozen_sources = tuple(sources)

        def capture(connection: sqlite3.Connection) -> dict[str, ResourceView]:
            assert_active()
            session = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if (
                session is None
                or session["owner_id"] != self.owner_id
                or not session["owner_active"]
                or session["owner_expires_at"] <= time()
            ):
                raise ExtensionRetired("Only the active host can capture extension resources")
            scopes = _service_scopes(session["principal_id"], session["project_id"], session_id)
            result: dict[str, ResourceView] = {}
            total_bytes = 0
            total_count = 0
            for source in frozen_sources:
                values: dict[ServiceScope, dict[str, ResourceVersion]] = {}
                for name, scope in scopes.items():
                    rows = connection.execute(
                        "SELECT r.resource_key,r.head_version,length(CAST(v.payload_json AS BLOB)) "
                        "FROM resources r LEFT JOIN resource_versions v ON "
                        "v.source_id=r.source_id AND v.scope=r.scope AND "
                        "v.resource_key=r.resource_key AND v.version=r.head_version "
                        "WHERE r.source_id=? AND r.scope=? AND r.head_version IS NOT NULL "
                        "ORDER BY r.resource_key LIMIT 1025",
                        (source, scope),
                    ).fetchall()
                    total_count += len(rows)
                    total_bytes += sum(row[2] or 0 for row in rows)
                    if total_count > 1024 or total_bytes > 8 * 1024 * 1024:
                        raise ValueError("Extension resource capture exceeds its size limit")
                    scoped: dict[str, ResourceVersion] = {}
                    for key, version, size in rows:
                        if size is None:
                            raise ValueError("Published extension resource version is missing")
                        body = connection.execute(
                            "SELECT payload_json FROM resource_versions WHERE source_id=? "
                            "AND scope=? AND resource_key=? AND version=?",
                            (source, scope, key, version),
                        ).fetchone()[0]
                        value = NamespaceResources._decode(version, body)
                        if value.key != key:
                            raise ValueError("Published extension resource key mismatch")
                        scoped[key] = value
                    values[name] = scoped
                result[source] = ResourceView(values)
            return result

        return await self.database.run(capture)

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
        inference: InferenceService | None = None,
    ) -> HostPublication:
        if (
            not generation
            or any(not source for source in sources)
            or len(set(sources)) != len(sources)
        ):
            raise ValueError("Invalid extension bindings")
        frozen_sources = tuple(sources)

        # One named unit of work: the extension rebind and its activation entry
        # commit together, so a generation can never be live without its marker,
        # nor a marker without its generation.
        session_row: list[sqlite3.Row] = []
        receipt: list[AppendReceipt | None] = []

        def validate(connection: sqlite3.Connection) -> None:
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
            session_row.append(session)

        def rebind(connection: sqlite3.Connection) -> None:
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

        def interrupt_tasks(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE extension_tasks SET status='interrupted',finished_at=? "
                "WHERE session_id=? AND owner_id<>? "
                "AND status IN ('queued','running','cancelling')",
                (time(), session_id, self.owner_id),
            )

        def commit_activation(connection: sqlite3.Connection) -> None:
            if activation is None:
                return
            if activation.token.session_id != session_id:
                raise ExtensionRetired("Resource activation belongs to another session")
            # The activation pointer is committed by the append below; a fault
            # here must prevent that commit rather than leave a half-published
            # version visible to the next session.
            self._hit_fault("activation_commit")
            receipt.append(
                SqliteSessionRepository(self.database).append_in_transaction(
                    connection,
                    (activation.entry,),
                    token=activation.token,
                    branch_id=activation.branch_id,
                    expected_head=activation.expected_head,
                )
            )

        await UnitOfWork(self.database).commit(
            CommitParticipant("extension-validation", validate),
            CommitParticipant("extension-rebind", rebind),
            CommitParticipant("task-interruption", interrupt_tasks),
            CommitParticipant("activation-entry", commit_activation),
        )
        session = session_row[0]
        principal_id, project_id = session["principal_id"], session["project_id"]
        publication_receipt = receipt[0] if receipt else None
        scopes = _service_scopes(principal_id, project_id, session_id)
        services: dict[str, HostServices] = {
            source: BoundHostServices(
                self.database,
                self.artifacts,
                ExtensionToken(session_id, source, self.owner_id, generation),
                scopes,
                assert_active,
                self.tasks,
                dict((handlers or {}).get(source, {})),
                inference=inference,
                maintenance=self.maintenance,
            )
            for source in frozen_sources
        }
        return HostPublication(services, publication_receipt)

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


def _service_scopes(principal: str, project: str, session: str) -> dict[ServiceScope, str]:
    return {
        "session": canonical_json(["session", principal, session]),
        "project": canonical_json(["project", principal, project]),
        "user": canonical_json(["user", principal]),
    }


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
        evaluation: EvaluationService | None = None,
        inference: InferenceService | None = None,
        maintenance: MaintenanceRegistry | None = None,
    ) -> None:
        self._assert_active = assert_active
        self._tasks = BoundTaskService(tasks, token, handlers, self, assert_active)
        self._snapshots = BoundSnapshots(database, token, assert_active)
        self._history = BoundHistory(database, token, assert_active)
        self._evaluation: EvaluationService = (
            evaluation if evaluation is not None else UnavailableEvaluation()
        )
        self._inference: InferenceService = (
            inference if inference is not None else UnavailableInference()
        )
        self._maintenance = maintenance or MaintenanceRegistry()
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
                sha256(canonical_json([token.source_id, scope]).encode()).hexdigest(),
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

    @property
    def history(self) -> HistoryService:
        self._assert_active()
        return self._history

    @property
    def evaluation(self) -> EvaluationService:
        """Report the composed evaluation capability, or its explicit absence."""
        self._assert_active()
        return self._evaluation

    @property
    def maintenance(self) -> MaintenanceRegistry:
        self._assert_active()
        return self._maintenance

    @property
    def inference(self) -> InferenceService:
        """Report the composed completion capability, or its explicit absence."""
        self._assert_active()
        return self._inference


class BoundHistory:
    def __init__(
        self, database: SqliteDatabase, token: ExtensionToken, assert_active: Callable[[], None]
    ) -> None:
        self._database, self._token, self._assert_active = database, token, assert_active

    async def read_custom(self, entry_id: str) -> CustomEntry:
        def read(connection: sqlite3.Connection) -> CustomEntry:
            self._assert_active()
            assert_extension(connection, self._token)
            row = connection.execute(
                "SELECT * FROM entries WHERE session_id=? AND entry_id=?",
                (self._token.session_id, entry_id),
            ).fetchone()
            if row is None:
                raise KeyError("Entry is missing or belongs to another session")
            entry = decode_entry(row)
            if not isinstance(entry, CustomEntry):
                raise ValueError("Entry is not a custom record")
            return entry

        return await self._database.run(read)

    async def read_completed_run(self, run_id: str) -> Sequence[SessionEntry]:
        def read(connection: sqlite3.Connection) -> tuple[SessionEntry, ...]:
            self._assert_active()
            assert_extension(connection, self._token)
            rows = connection.execute(
                "SELECT * FROM entries WHERE session_id=? AND run_id=? ORDER BY seq",
                (self._token.session_id, run_id),
            ).fetchall()
            return tuple(decode_entry(row) for row in rows)

        return await self._database.run(read)


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
