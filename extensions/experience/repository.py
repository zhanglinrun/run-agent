"""Immutable candidates and atomic publication through scoped host services."""

from __future__ import annotations

import hashlib
from time import time
from typing import cast
from uuid import uuid4

from run_agent_coding.host.contracts import (
    HeadChange,
    HostServices,
    ResourceVersion,
    ScopedServices,
    StateChange,
)
from run_agent_coding.host.learning import require_writeback
from run_agent_core.session.contracts import SessionConflict
from run_agent_core.types import JSONValue

from .models import AssetKind, Candidate, Proposal, Scope, asset_key


class ExperienceRepository:
    def __init__(self, services: HostServices, session_id: str) -> None:
        self.services, self.session_id = services, session_id

    async def initialize(self) -> None:
        for scope in ("project", "user"):
            state = self.services.scope(scope).state
            value = await state.get("schema")
            if value is None:
                try:
                    await state.compare_and_set(StateChange("schema", 0, {"version": 1}))
                except SessionConflict:
                    value = await state.get("schema")
                else:
                    continue
            if value is None or value.value != {"version": 1}:
                raise ValueError("Unsupported experience namespace schema")

    def scoped(self, scope: Scope) -> ScopedServices:
        if scope not in {"project", "user"}:
            raise ValueError("Experience scope must be project or user")
        return self.services.scope(scope)

    async def _command(self, entry_id: str, scope: Scope, actions: set[str]) -> None:
        entry = await self.services.history.read_custom(entry_id)
        if (
            entry.namespace != "experience.command"
            or entry.data.get("scope") != scope
            or entry.data.get("action") not in actions
        ):
            raise ValueError("Experience command evidence does not authorize this operation")

    async def propose(
        self,
        proposal: Proposal,
        *,
        command_id: str | None = None,
        snapshot_id: str | None = None,
        expected_base: str | None = None,
    ) -> Candidate:
        # P5-4: an evaluation must not change the asset it is measuring.
        require_writeback()
        scoped = self.scoped(proposal.scope)
        if command_id is not None:
            await self._command(command_id, proposal.scope, {"remember", "propose", "import"})
        snapshot = await self.services.snapshots.read(snapshot_id) if snapshot_id else None
        if command_id is None and snapshot is None:
            raise ValueError("A proposal requires a source command or a fixed input snapshot")
        key = asset_key(proposal.kind, proposal.name)
        heads = await scoped.resources.snapshot()
        base = heads.get(key)
        if expected_base is not None and base != expected_base:
            raise SessionConflict("Working-copy base changed; review a new checkout")
        version = await scoped.resources.put_immutable(
            key,
            proposal.content,
            parent_version=base,
            metadata={
                "kind": proposal.kind,
                "description": proposal.description or proposal.name,
                "source_kind": "manual" if command_id else "model",
                "source_session": self.session_id,
                "source_command": command_id,
                "source_snapshot": snapshot_id,
                "source_run": snapshot.run_id if snapshot else None,
                "observed_at": time(),
                "applies_to": list(proposal.applies_to),
                "invalidation_conditions": list(proposal.invalidation_conditions),
                "expires_at": proposal.expires_at,
                "active": True,
            },
        )
        candidate = Candidate(
            candidate_id=uuid4().hex,
            asset_id=key,
            kind=proposal.kind,
            scope=proposal.scope,
            base_version=base,
            content_version=version.version,
            content_hash=hashlib.sha256(proposal.content.encode()).hexdigest(),
            source_session=self.session_id,
            source_kind="manual" if command_id else "model",
            source_command=command_id,
            source_snapshot=snapshot_id,
            source_run=snapshot.run_id if snapshot else None,
            observed_at=time(),
            applies_to=proposal.applies_to,
            invalidation_conditions=proposal.invalidation_conditions,
            status="proposed" if command_id else "needs_evidence",
        )
        await scoped.state.compare_and_set(
            StateChange(
                "candidate/" + candidate.candidate_id,
                0,
                cast(JSONValue, candidate.model_dump(mode="json")),
            )
        )
        return candidate

    async def candidate(self, scope: Scope, candidate_id: str) -> Candidate:
        row = await self.scoped(scope).state.get("candidate/" + candidate_id)
        if row is None:
            raise KeyError("Unknown experience candidate")
        return Candidate.model_validate(row.value)

    async def candidates(self, scope: Scope) -> list[Candidate]:
        return [
            Candidate.model_validate(row.value)
            for row in await self.scoped(scope).state.list(prefix="candidate/", limit=1000)
        ]

    async def publish_manual(self, scope: Scope, candidate_id: str, command_id: str) -> Candidate:
        if not command_id:
            raise ValueError("Manual publication requires an explicit command receipt")
        await self._command(command_id, scope, {"remember", "publish"})
        scoped = self.scoped(scope)
        key = "candidate/" + candidate_id
        row = await scoped.state.get(key)
        if row is None:
            raise KeyError("Unknown experience candidate")
        candidate = Candidate.model_validate(row.value)
        if candidate.status not in {"proposed", "needs_evidence"}:
            raise ValueError(f"Candidate is {candidate.status}")
        value = await scoped.resources.resolve(candidate.asset_id, candidate.content_version)
        if hashlib.sha256(value.content.encode()).hexdigest() != candidate.content_hash:
            raise ValueError("Candidate content hash mismatch")
        current = (await scoped.resources.snapshot()).get(candidate.asset_id)
        if current != candidate.base_version:
            await scoped.state.compare_and_set(
                StateChange(
                    key,
                    row.version,
                    cast(
                        JSONValue,
                        candidate.model_copy(update={"status": "stale"}).model_dump(mode="json"),
                    ),
                )
            )
            raise SessionConflict("Candidate base changed; create a new candidate")
        result = candidate.model_copy(update={"status": "promoted"})
        await scoped.state.apply_batch(
            states=[StateChange(key, row.version, cast(JSONValue, result.model_dump(mode="json")))],
            heads=[
                HeadChange(
                    candidate.asset_id,
                    candidate.base_version,
                    candidate.content_version,
                    "manual",
                    {"candidate_id": candidate_id, "command_id": command_id},
                )
            ],
        )
        return result

    async def forget(
        self, scope: Scope, kind: AssetKind, name: str, command_id: str
    ) -> ResourceVersion:
        await self._command(command_id, scope, {"forget"})
        scoped = self.scoped(scope)
        key = asset_key(kind, name)
        current = (await scoped.resources.snapshot()).get(key)
        if current is None:
            raise KeyError("Unknown experience asset")
        tombstone = await scoped.resources.put_immutable(
            key,
            "",
            parent_version=current,
            metadata={"active": False, "source_command": command_id, "observed_at": time()},
        )
        await scoped.resources.advance_head(
            HeadChange(
                key,
                current,
                tombstone.version,
                "forgotten",
                {"command_id": command_id},
            )
        )
        return tombstone

    async def rollback(
        self, scope: Scope, key: str, version: str, command_id: str
    ) -> ResourceVersion:
        await self._command(command_id, scope, {"rollback"})
        scoped = self.scoped(scope)
        previous = await scoped.resources.resolve(key, version)
        current = (await scoped.resources.snapshot()).get(key)
        if current is None:
            raise KeyError("Unknown experience asset")
        await scoped.resources.advance_head(
            HeadChange(
                key,
                current,
                version,
                "manual_rollback",
                {"command_id": command_id},
            )
        )
        return previous

    async def search(self, scope: Scope, query: str = "") -> list[ResourceVersion]:
        resources = self.scoped(scope).resources
        result = []
        for key, version in (await resources.snapshot()).items():
            resource = await resources.resolve(key, version)
            if not resource.metadata.get("active", False):
                continue
            if query.casefold() in (key + "\n" + resource.content).casefold():
                result.append(resource)
            if len(result) == 100:
                break
        return result
