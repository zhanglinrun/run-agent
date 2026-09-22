"""In-memory extension host services (no SQLite)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path

from run_agent_coding.host.context_resources import ResourceView
from run_agent_coding.host.contracts import (
    ArtifactRef,
    ContextSnapshot,
    ExtensionToken,
    HeadChange,
    HistoryService,
    HostPublication,
    HostServices,
    ResourceVersion,
    ScopedServices,
    ServiceScope,
    SessionActivation,
    SnapshotService,
    StateChange,
    StateValue,
    TaskHandler,
    TaskService,
)
from run_agent_coding.host.evaluation import EvaluationService, UnavailableEvaluation
from run_agent_coding.host.inference import InferenceService, UnavailableInference
from run_agent_coding.host.maintenance import MaintenanceRegistry
from run_agent_coding.jsonl_storage import SessionWriter
from run_agent_coding.storage.canonical import canonical_json
from run_agent_coding.storage.tasks import BoundTaskService, LocalTaskManager
from run_agent_core.session.contracts import SessionConflict
from run_agent_core.session.entries import CustomEntry, LeafEntry, RunCommitEntry, SessionEntry
from run_agent_core.session.tree import SessionTreeError, path_to_entry
from run_agent_core.types import JSONValue

# Process-local backing stores, isolated by application home so two SessionManagers
# that share ~/.run can see the same project/user state. Session scope stays per id.
_SCOPE_STATE: dict[tuple[object, ...], dict[str, StateValue]] = {}
_SCOPE_VERSIONS: dict[tuple[object, ...], dict[str, ResourceVersion]] = {}
_SCOPE_HEADS: dict[tuple[object, ...], dict[str, str]] = {}
_SCOPE_BLOBS: dict[tuple[object, ...], dict[str, bytes]] = {}


class ExtensionRetired(SessionConflict):
    """This source instance has been unloaded, replaced or closed."""


class MemoryHostServices:
    def __init__(
        self,
        owner_id: str,
        *,
        isolation_key: str = "",
        principal_id: str = "local",
        evaluation: EvaluationService | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.isolation_key = isolation_key or owner_id
        self.principal_id = principal_id
        self.tasks = LocalTaskManager()
        self.evaluation: EvaluationService = evaluation or UnavailableEvaluation()
        self.maintenance = MaintenanceRegistry()
        self.fault: Callable[[str], None] | None = None
        self._writers: dict[str, SessionWriter] = {}
        self._cwd_by_session: dict[str, str] = {}

    def attach_writer(self, writer: SessionWriter, *, cwd: Path | None = None) -> None:
        self._writers[writer.session_id] = writer
        if cwd is not None:
            self._cwd_by_session[writer.session_id] = str(cwd.resolve())

    def _scope_key(self, session_id: str, source_id: str, scope: str) -> tuple[object, ...]:
        cwd = self._cwd_by_session.get(session_id, "")
        if scope == "session":
            return (self.isolation_key, "session", session_id, source_id)
        if scope == "project":
            return (self.isolation_key, "project", cwd, source_id)
        return (self.isolation_key, "user", self.principal_id, source_id)

    async def capture_resources(
        self, session_id: str, sources: Sequence[str], assert_active: Callable[[], None]
    ) -> Mapping[str, ResourceView]:
        assert_active()
        views: dict[str, ResourceView] = {}
        for source in sources:
            names: tuple[ServiceScope, ...] = ("session", "project", "user")
            scopes: dict[ServiceScope, dict[str, ResourceVersion]] = {}
            for name in names:
                key = self._scope_key(session_id, source, name)
                heads = _SCOPE_HEADS.get(key, {})
                versions = _SCOPE_VERSIONS.get(key, {})
                scopes[name] = {
                    resource_key: versions[version]
                    for resource_key, version in heads.items()
                    if version in versions
                }
            views[source] = ResourceView(scopes)
        return views

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
        del expected_generation
        if (
            not generation
            or any(not source for source in sources)
            or len(set(sources)) != len(sources)
        ):
            raise ValueError("Invalid extension bindings")
        assert_active()
        writer = self._writers.get(session_id)
        receipt = None
        if activation is not None:
            if writer is None:
                raise RuntimeError("Session writer is not attached")
            if self.fault is not None:
                self.fault("activation_commit")
            leaf = LeafEntry(parent_id=activation.entry.id, entry_id=activation.entry.id)
            receipt = await writer.append_entries(
                (activation.entry, leaf),
                expected_head=activation.expected_head,
                token=activation.token,
            )
        services: dict[str, HostServices] = {
            source: BoundHostServices(
                ExtensionToken(session_id, source, self.owner_id, generation),
                assert_active,
                self.tasks,
                dict((handlers or {}).get(source, {})),
                self._scope_bundle(session_id, source, assert_active),
                writer,
                inference=inference,
                evaluation=self.evaluation,
                maintenance=self.maintenance,
            )
            for source in sources
        }
        return HostPublication(services, receipt)

    def _scope_bundle(
        self, session_id: str, source_id: str, assert_active: Callable[[], None]
    ) -> dict[str, ScopedServices]:
        scopes: dict[str, ScopedServices] = {}
        for name in ("session", "project", "user"):
            key = self._scope_key(session_id, source_id, name)
            values = _SCOPE_STATE.setdefault(key, {})
            versions = _SCOPE_VERSIONS.setdefault(key, {})
            heads = _SCOPE_HEADS.setdefault(key, {})
            blobs = _SCOPE_BLOBS.setdefault(key, {})
            artifacts = MemoryArtifacts(blobs, assert_active)
            scopes[name] = ScopedServices(
                MemoryState(values, assert_active),
                MemoryResources(versions, heads, blobs, assert_active),
                artifacts,
                sha256(canonical_json([source_id, name]).encode()).hexdigest(),
            )
        return scopes

    async def retire(self, session_id: str, generation: str) -> int:
        return await self.tasks.retire(session_id, generation)


class BoundHostServices:
    def __init__(
        self,
        token: ExtensionToken,
        assert_active: Callable[[], None],
        tasks: LocalTaskManager,
        handlers: dict[str, TaskHandler],
        scopes: dict[str, ScopedServices],
        writer: SessionWriter | None,
        evaluation: EvaluationService | None = None,
        inference: InferenceService | None = None,
        maintenance: MaintenanceRegistry | None = None,
    ) -> None:
        self._assert_active = assert_active
        self._tasks = BoundTaskService(tasks, token, handlers, self, assert_active)
        self._snapshots = WriterSnapshots(writer)
        self._history = WriterHistory(writer, assert_active)
        self._evaluation: EvaluationService = evaluation or UnavailableEvaluation()
        self._inference: InferenceService = inference or UnavailableInference()
        self._maintenance = maintenance or MaintenanceRegistry()
        self._scopes = scopes

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
        self._assert_active()
        return self._evaluation

    @property
    def maintenance(self) -> MaintenanceRegistry:
        self._assert_active()
        return self._maintenance

    @property
    def inference(self) -> InferenceService:
        self._assert_active()
        return self._inference


class WriterHistory:
    def __init__(self, writer: SessionWriter | None, assert_active: Callable[[], None]) -> None:
        self._writer = writer
        self._assert_active = assert_active

    async def _entries(self) -> list[SessionEntry]:
        if self._writer is None:
            return []
        return await self._writer.read_all()

    async def read_custom(self, entry_id: str) -> CustomEntry:
        self._assert_active()
        for entry in await self._entries():
            if entry.id == entry_id and isinstance(entry, CustomEntry):
                return entry
        raise KeyError("Entry is missing or belongs to another session")

    async def read_completed_run(self, run_id: str) -> Sequence[SessionEntry]:
        self._assert_active()
        entries = await self._entries()
        commits = [
            entry
            for entry in entries
            if isinstance(entry, RunCommitEntry) and entry.run_id == run_id
        ]
        if not commits:
            raise KeyError(f"Unknown or incomplete run: {run_id}")
        if len(commits) > 1:
            raise KeyError(f"Ambiguous completed run: {run_id}")
        commit = commits[0]
        if commit.end_entry_id is None:
            if commit.start_entry_id is None:
                return ()
            raise KeyError(f"Run has invalid durable boundaries: {run_id}")
        try:
            path = path_to_entry(entries, commit.end_entry_id)
        except SessionTreeError as exc:
            raise KeyError(f"Run has invalid durable boundaries: {run_id}") from exc
        if commit.start_entry_id is None:
            return tuple(path)
        try:
            start_index = next(
                index for index, entry in enumerate(path) if entry.id == commit.start_entry_id
            )
        except StopIteration as exc:
            raise KeyError(f"Run has invalid durable boundaries: {run_id}") from exc
        return tuple(path[start_index + 1 :])


class WriterSnapshots:
    def __init__(self, writer: SessionWriter | None) -> None:
        self._writer = writer

    async def read(self, snapshot_id: str) -> ContextSnapshot:
        if self._writer is None:
            raise KeyError(snapshot_id)
        payload = self._writer.snapshots.get(snapshot_id)
        if payload is None:
            raise KeyError(snapshot_id)
        numbered = await self._writer.read_all()
        return ContextSnapshot(
            snapshot_id,
            self._writer.session_id,
            self._writer.token.run_id,
            self._writer.branch_id,
            numbered[-1].id if numbered else None,
            len(numbered),
            "coding-input-v1",
            "",
            payload,
        )


class MemoryState:
    def __init__(self, values: dict[str, StateValue], assert_active: Callable[[], None]) -> None:
        self._values = values
        self._assert_active = assert_active

    async def get(self, key: str) -> StateValue | None:
        self._assert_active()
        return self._values.get(key)

    async def list(self, *, prefix: str = "", limit: int = 100) -> list[StateValue]:
        self._assert_active()
        rows = [value for key, value in sorted(self._values.items()) if key.startswith(prefix)]
        return rows[:limit]

    async def compare_and_set(self, change: StateChange) -> StateValue:
        self._assert_active()
        current = self._values.get(change.key)
        version = 0 if current is None else current.version
        if version != change.expected_version:
            raise SessionConflict("State version conflict")
        value = StateValue(change.key, version + 1, change.value)
        self._values[change.key] = value
        return value

    async def apply_batch(
        self, states: Sequence[StateChange] = (), heads: Sequence[HeadChange] = ()
    ) -> None:
        del heads
        for change in states:
            await self.compare_and_set(change)


class MemoryResources:
    def __init__(
        self,
        versions: dict[str, ResourceVersion],
        heads: dict[str, str],
        blobs: dict[str, bytes],
        assert_active: Callable[[], None],
    ) -> None:
        self._versions = versions
        self._heads = heads
        self._blobs = blobs
        self._assert_active = assert_active

    async def put_immutable(
        self,
        key: str,
        content: str,
        *,
        parent_version: str | None = None,
        metadata: dict[str, JSONValue] | None = None,
        artifacts: Sequence[ArtifactRef] = (),
    ) -> ResourceVersion:
        self._assert_active()
        refs = tuple(sorted(artifacts, key=lambda item: item.digest))
        if len({ref.digest for ref in refs}) != len(refs):
            raise ValueError("Duplicate resource artifacts")
        for ref in refs:
            if ref.digest not in self._blobs:
                raise KeyError(f"Artifact is outside this source scope: {ref.digest}")
        value = ResourceVersion(key, "", parent_version, content, dict(metadata or {}), refs)
        body = asdict(value)
        body.pop("version")
        version = sha256(canonical_json(body).encode()).hexdigest()
        value = ResourceVersion(key, version, parent_version, content, dict(metadata or {}), refs)
        self._versions[version] = value
        self._heads[key] = version
        return value

    async def resolve(self, key: str, version: str) -> ResourceVersion:
        self._assert_active()
        value = self._versions.get(version)
        if value is None or value.key != key:
            raise KeyError(key)
        return value

    async def snapshot(self) -> dict[str, str]:
        self._assert_active()
        return dict(self._heads)

    async def advance_head(self, change: HeadChange) -> None:
        self._assert_active()
        self._heads[change.key] = change.version


class MemoryArtifacts:
    def __init__(self, blobs: dict[str, bytes], assert_active: Callable[[], None]) -> None:
        self._blobs = blobs
        self._assert_active = assert_active

    async def put(self, content: bytes) -> ArtifactRef:
        self._assert_active()
        digest = sha256(content).hexdigest()
        self._blobs[digest] = content
        return ArtifactRef(digest, len(content))

    async def read(self, ref: ArtifactRef) -> bytes:
        self._assert_active()
        try:
            return self._blobs[ref.digest]
        except KeyError as exc:
            raise KeyError(f"Artifact is outside this source scope: {ref.digest}") from exc
