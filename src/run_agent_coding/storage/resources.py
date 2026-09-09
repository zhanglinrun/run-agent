"""Immutable version storage; live pointers change only through explicit CAS."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from time import time

from run_agent_coding.host.contracts import ArtifactRef, ExtensionToken, HeadChange, ResourceVersion
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import NamespaceState, assert_extension
from run_agent_core.session.contracts import SessionConflict
from run_agent_core.types import JSONValue


class NamespaceResources:
    def __init__(
        self,
        database: SqliteDatabase,
        token: ExtensionToken,
        scope: str,
        artifacts: ArtifactStore,
        assert_active: Callable[[], None] = lambda: None,
        artifact_reader: Callable[[ArtifactRef], Awaitable[bytes]] | None = None,
    ) -> None:
        self.database = database
        self.token = token
        self.scope = scope
        self.artifacts = artifacts
        self.assert_active = assert_active
        self._read_artifact = artifact_reader or artifacts.read

    async def put_immutable(
        self,
        key: str,
        content: str,
        *,
        parent_version: str | None = None,
        metadata: dict[str, JSONValue] | None = None,
        artifacts: Sequence[ArtifactRef] = (),
    ) -> ResourceVersion:
        if not key:
            raise ValueError("A resource key is required")
        refs = tuple(sorted(artifacts, key=lambda item: item.digest))
        if len({ref.digest for ref in refs}) != len(refs):
            raise ValueError("Duplicate resource artifacts")
        body = canonical_json(
            {
                "key": key,
                "parent_version": parent_version,
                "content": content,
                "metadata": metadata or {},
                "artifacts": [asdict(ref) for ref in refs],
            }
        )
        version = hashlib.sha256(body.encode()).hexdigest()
        # Files are durable before a database reference can become visible.
        for ref in refs:
            await self._read_artifact(ref)

        def put(connection: sqlite3.Connection) -> ResourceVersion:
            self.assert_active()
            assert_extension(connection, self.token)
            connection.execute(
                "INSERT OR IGNORE INTO resources VALUES (?, ?, ?, NULL)",
                (self.token.source_id, self.scope, key),
            )
            if (
                parent_version is not None
                and connection.execute(
                    """SELECT 1 FROM resource_versions WHERE source_id=? AND scope=?
                   AND resource_key=? AND version=?""",
                    (self.token.source_id, self.scope, key, parent_version),
                ).fetchone()
                is None
            ):
                raise SessionConflict("Resource parent version does not exist")
            existing = connection.execute(
                """SELECT payload_json FROM resource_versions WHERE source_id=? AND scope=?
                   AND resource_key=? AND version=?""",
                (self.token.source_id, self.scope, key, version),
            ).fetchone()
            if existing is not None and existing[0] != body:
                raise SessionConflict("Immutable resource content mismatch")
            connection.execute(
                "INSERT OR IGNORE INTO resource_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.token.source_id, self.scope, key, version, parent_version, body, time()),
            )
            owner_key = canonical_json([self.token.source_id, self.scope, key, version])
            for ref in refs:
                stored = connection.execute(
                    "SELECT size FROM artifacts WHERE digest=?", (ref.digest,)
                ).fetchone()
                if stored is not None and stored[0] != ref.size:
                    raise SessionConflict("Artifact metadata mismatch")
                connection.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?)",
                    (ref.digest, ref.size, time()),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO artifact_refs VALUES ('resource', ?, ?)",
                    (owner_key, ref.digest),
                )
            return self._decode(version, body)

        return await self.database.run(put, write=True)

    @staticmethod
    def _decode(version: str, body: str) -> ResourceVersion:
        if hashlib.sha256(body.encode()).hexdigest() != version:
            raise SessionConflict("Immutable resource hash mismatch")
        payload = json.loads(body)
        return ResourceVersion(
            key=payload["key"],
            version=version,
            parent_version=payload["parent_version"],
            content=payload["content"],
            metadata=payload["metadata"],
            artifacts=tuple(ArtifactRef(**item) for item in payload["artifacts"]),
        )

    async def resolve(self, key: str, version: str) -> ResourceVersion:
        def resolve(connection: sqlite3.Connection) -> ResourceVersion:
            self.assert_active()
            assert_extension(connection, self.token)
            row = connection.execute(
                """SELECT payload_json FROM resource_versions WHERE source_id=? AND scope=?
                   AND resource_key=? AND version=?""",
                (self.token.source_id, self.scope, key, version),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown resource version: {key}/{version}")
            return self._decode(version, row[0])

        return await self.database.run(resolve)

    async def snapshot(self) -> dict[str, str]:
        def snapshot(connection: sqlite3.Connection) -> dict[str, str]:
            self.assert_active()
            assert_extension(connection, self.token)
            rows = connection.execute(
                """SELECT resource_key, head_version FROM resources WHERE source_id=? AND scope=?
                   AND head_version IS NOT NULL ORDER BY resource_key""",
                (self.token.source_id, self.scope),
            ).fetchall()
            return {row[0]: row[1] for row in rows}

        return await self.database.run(snapshot)

    async def advance_head(self, change: HeadChange) -> None:
        await NamespaceState(
            self.database,
            self.token,
            self.scope,
            self.assert_active,
        ).apply_batch(heads=[change])
