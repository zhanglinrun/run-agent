"""Read-only resource selection during staged Session activation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from run_agent_coding.host.contracts import ResourceVersion, ServiceScope


@dataclass(frozen=True, slots=True)
class ResourceSelection:
    scope: ServiceScope
    key: str
    version: str
    title: str
    kind: Literal["context", "instructions"] = "context"
    max_tokens: int = 2048


class ResourceView:
    """One source's published heads, captured together across all its scopes.

    No live reads, task submission, or write capabilities are exposed. Returned
    values are copies so a selector cannot alter the host's captured content.
    """

    def __init__(self, values: Mapping[ServiceScope, Mapping[str, ResourceVersion]]) -> None:
        self._values = deepcopy(dict(values))

    def heads(self, scope: ServiceScope = "session") -> dict[str, str]:
        return {key: value.version for key, value in self._values.get(scope, {}).items()}

    def resolve(self, scope: ServiceScope, key: str, version: str) -> ResourceVersion:
        value = self._values.get(scope, {}).get(key)
        if value is None or value.version != version:
            raise KeyError(f"Resource is outside the captured heads: {scope}/{key}/{version}")
        return deepcopy(value)


ResourceProvider = Callable[[ResourceView], Sequence[ResourceSelection]]


@dataclass(frozen=True, slots=True)
class ResourceProviderIdentity:
    source_id: str
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ContextResource:
    provider: ResourceProviderIdentity
    selection: ResourceSelection
    resource: ResourceVersion


@dataclass(frozen=True, slots=True)
class ExtensionResourceSnapshot:
    providers: tuple[ResourceProviderIdentity, ...]
    contributions: tuple[ContextResource, ...]
