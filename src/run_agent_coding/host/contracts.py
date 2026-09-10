"""Typed services injected into extensions, with host-bound identity and scope."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from run_agent_core.session.contracts import AppendReceipt, RunToken
from run_agent_core.session.entries import CustomEntry
from run_agent_core.types import JSONValue

if TYPE_CHECKING:
    from run_agent_coding.host.context_resources import ResourceView


@dataclass(frozen=True, slots=True)
class ExtensionToken:
    session_id: str
    source_id: str
    owner_id: str
    generation: str


@dataclass(frozen=True, slots=True)
class StateValue:
    key: str
    version: int
    value: JSONValue


@dataclass(frozen=True, slots=True)
class StateChange:
    key: str
    expected_version: int
    value: JSONValue


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class ResourceVersion:
    key: str
    version: str
    parent_version: str | None
    content: str
    metadata: dict[str, JSONValue]
    artifacts: tuple[ArtifactRef, ...]


@dataclass(frozen=True, slots=True)
class HeadChange:
    key: str
    expected_version: str | None
    version: str
    reason: str
    evidence: dict[str, JSONValue]


class StateService(Protocol):
    async def get(self, key: str) -> StateValue | None: ...

    async def list(self, *, prefix: str = "", limit: int = 100) -> list[StateValue]: ...

    async def compare_and_set(self, change: StateChange) -> StateValue: ...

    async def apply_batch(
        self, states: Sequence[StateChange] = (), heads: Sequence[HeadChange] = ()
    ) -> None: ...


class ResourceService(Protocol):
    async def put_immutable(
        self,
        key: str,
        content: str,
        *,
        parent_version: str | None = None,
        metadata: dict[str, JSONValue] | None = None,
        artifacts: Sequence[ArtifactRef] = (),
    ) -> ResourceVersion: ...

    async def resolve(self, key: str, version: str) -> ResourceVersion: ...

    async def snapshot(self) -> dict[str, str]: ...

    async def advance_head(self, change: HeadChange) -> None: ...


class ArtifactService(Protocol):
    async def put(self, content: bytes) -> ArtifactRef: ...

    async def read(self, ref: ArtifactRef) -> bytes: ...


ServiceScope = Literal["session", "project", "user"]


@dataclass(frozen=True, slots=True)
class ScopedServices:
    state: StateService
    resources: ResourceService
    artifacts: ArtifactService
    projection_key: str


class HostServices(Protocol):
    @property
    def tasks(self) -> TaskService: ...

    @property
    def snapshots(self) -> SnapshotService: ...

    @property
    def history(self) -> HistoryService: ...

    def scope(self, scope: ServiceScope = "session") -> ScopedServices:
        """Choose one host-bound scope; identities cannot be supplied by tools."""
        ...


class HostServicesRegistry(Protocol):
    async def capture_resources(
        self, session_id: str, sources: Sequence[str], assert_active: Callable[[], None]
    ) -> Mapping[str, ResourceView]:
        """Capture published resources without activating a staged extension."""
        ...

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
    ) -> HostPublication: ...

    async def retire(self, session_id: str, generation: str) -> int:
        """Revoke writes and drain tasks; return the number still cancelling."""
        ...


@dataclass(frozen=True, slots=True)
class TaskSpec:
    handler: str
    payload: JSONValue
    snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskInfo:
    task_id: str
    handler: str
    status: str
    result: JSONValue = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TaskContext:
    task_id: str
    snapshot_id: str | None
    services: HostServices


TaskHandler = Callable[[JSONValue, TaskContext], Awaitable[JSONValue]]


class TaskService(Protocol):
    async def submit(self, spec: TaskSpec) -> str: ...

    async def status(self, task_id: str) -> TaskInfo: ...

    async def cancel(self, task_id: str) -> TaskInfo: ...


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    snapshot_id: str
    session_id: str
    run_id: str
    branch_id: str
    head_id: str | None
    watermark: int
    builder_version: str
    content_hash: str
    payload: dict[str, JSONValue]


class SnapshotService(Protocol):
    async def read(self, snapshot_id: str) -> ContextSnapshot:
        """Read a verified, fixed input belonging to this service's session."""
        ...


class HistoryService(Protocol):
    async def read_custom(self, entry_id: str) -> CustomEntry:
        """Read a persisted custom entry belonging to this Session."""
        ...


@dataclass(frozen=True, slots=True)
class SessionActivation:
    token: RunToken
    branch_id: str
    expected_head: str | None
    entry: CustomEntry


@dataclass(frozen=True, slots=True)
class HostPublication:
    services: Mapping[str, HostServices]
    activation_receipt: AppendReceipt | None
