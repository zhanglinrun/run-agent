"""Stage and validate fixed context contributions without running live writes."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from inspect import isawaitable, iscoroutine, iscoroutinefunction
from typing import cast

from pydantic import TypeAdapter

from run_agent_coding.context_window import estimate_text_tokens
from run_agent_coding.host.context_resources import (
    ContextResource,
    ExtensionResourceSnapshot,
    ResourceProvider,
    ResourceProviderIdentity,
    ResourceSelection,
    ResourceView,
)
from run_agent_coding.storage.canonical import canonical_json
from run_agent_coding.system_prompt import PromptSection
from run_agent_core.types import JSONValue


class ContextResourceProviders:
    def __init__(self) -> None:
        self.registrations: dict[ResourceProviderIdentity, ResourceProvider] = {}
        self.snapshot: ExtensionResourceSnapshot | None = None

    def register(self, source: str, name: str, version: str, provider: ResourceProvider) -> None:
        if self.snapshot is not None:
            raise ValueError("Resource providers must be registered before resource preparation")
        if not name or not version or len(name.encode()) > 128 or len(version.encode()) > 128:
            raise ValueError("Resource providers need a bounded name and version")
        if any(item.source_id == source and item.name == name for item in self.registrations):
            raise ValueError(f"Duplicate resource provider: {name}")
        if not callable(provider) or iscoroutinefunction(provider):
            raise ValueError("Resource providers must be synchronous selectors")
        self.registrations[ResourceProviderIdentity(source, name, version)] = provider

    def remove(self, source: str) -> None:
        self.registrations = {
            key: value for key, value in self.registrations.items() if key.source_id != source
        }

    def capture(self, views: dict[str, ResourceView]) -> ExtensionResourceSnapshot:
        contributions = []
        for identity, provider in self.registrations.items():
            # Selectors are synchronous, bounded transformations of detached data.
            view = views[identity.source_id]
            selections = provider(view)
            if isawaitable(selections):
                if iscoroutine(selections):
                    selections.close()
                raise ValueError("Resource providers must be synchronous selectors")
            if not isinstance(selections, (tuple, list)) or len(selections) > 64:
                raise ValueError("Resource providers must return at most 64 selections")
            for selection in selections:
                if not isinstance(selection, ResourceSelection):
                    raise ValueError("Resource providers must return ResourceSelection values")
                resource = view.resolve(selection.scope, selection.key, selection.version)
                contributions.append(ContextResource(identity, selection, resource))
        result = ExtensionResourceSnapshot(tuple(self.registrations), tuple(contributions))
        self.validate(result)
        return result

    def decode(self, payload: JSONValue) -> ExtensionResourceSnapshot:
        result = TypeAdapter(ExtensionResourceSnapshot).validate_python(payload)
        self.validate(result)
        return result

    def validate(self, snapshot: ExtensionResourceSnapshot) -> None:
        if snapshot.providers != tuple(self.registrations):
            raise ValueError("Resource providers differ from the pinned snapshot")
        if len(snapshot.contributions) > 128:
            raise ValueError("Too many extension context resources")
        payload = TypeAdapter(ExtensionResourceSnapshot).dump_python(snapshot, mode="json")
        if len(canonical_json(payload).encode()) > 512 * 1024:
            raise ValueError("Extension context resource snapshot exceeds its size limit")
        seen = set()
        total_tokens = 0
        total_bytes = 0
        for item in snapshot.contributions:
            selected, resource = item.selection, item.resource
            identity = (item.provider, selected.scope, selected.key)
            if identity in seen or item.provider not in self.registrations:
                raise ValueError("Duplicate or unknown resource provider contribution")
            seen.add(identity)
            if (
                selected.scope not in {"session", "project", "user"}
                or selected.kind not in {"context", "instructions"}
                or selected.presentation not in {"content", "index"}
                or not selected.title
                or len(selected.title) > 128
                or "\n" in selected.title
                or "\r" in selected.title
                or not 1 <= selected.max_tokens <= 8192
            ):
                raise ValueError("Invalid extension context resource metadata or budget")
            body = asdict(resource)
            body.pop("version")
            if (
                resource.version != selected.version
                or resource.key != selected.key
                or sha256(canonical_json(body).encode()).hexdigest() != resource.version
            ):
                raise ValueError("Pinned extension resource content hash mismatch")
            prompt_content = item.prompt_content()
            tokens = estimate_text_tokens(selected.title + "\n" + prompt_content)
            total_tokens += tokens
            total_bytes += len(prompt_content.encode())
            if tokens > selected.max_tokens or total_tokens > 16384 or total_bytes > 128 * 1024:
                raise ValueError("Extension context resource exceeds its context budget")

    @property
    def sections(self) -> tuple[PromptSection, ...]:
        if self.snapshot is None:
            return ()
        return tuple(
            PromptSection(item.selection.title, item.prompt_content())
            for item in self.snapshot.contributions
        )

    def payload(self) -> JSONValue:
        if self.snapshot is None:
            raise ValueError("Extension resources have not been prepared")
        return cast(
            JSONValue,
            TypeAdapter(ExtensionResourceSnapshot).dump_python(self.snapshot, mode="json"),
        )
