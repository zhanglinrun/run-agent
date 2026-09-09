"""Typed services injected into extensions, with host-bound identity and scope."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from run_agent_core.types import JSONValue


@dataclass(frozen=True, slots=True)
class ExtensionToken:
    session_id: str
    source_id: str
    owner_id: str
    generation: int


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
