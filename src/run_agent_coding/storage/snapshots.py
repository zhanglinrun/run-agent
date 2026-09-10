"""Content-addressed context blocks shared by successive model-input snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from run_agent_core.session.contracts import SessionConflict


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def encode_context(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Deduplicate individual messages/tools and stable system/resource inputs."""
    blocks: dict[str, str] = {}

    def block(value: Any) -> str:
        body = _json(value)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        blocks[digest] = body
        return digest

    manifest = {
        key: {"items": [block(item) for item in value]}
        if isinstance(value, list)
        else {"value": block(value)}
        for key, value in payload.items()
    }
    return _json(manifest), blocks


def store_blocks(connection: sqlite3.Connection, blocks: dict[str, str]) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO snapshot_blocks(digest,body_json) VALUES (?,?)",
        blocks.items(),
    )
    for digest, body in blocks.items():
        stored = connection.execute(
            "SELECT body_json FROM snapshot_blocks WHERE digest=?",
            (digest,),
        ).fetchone()
        if stored is None or stored[0] != body:
            raise SessionConflict("Context block content hash mismatch")


def decode_context(connection: sqlite3.Connection, manifest: str) -> dict[str, Any]:
    blocks: dict[str, Any] = {}

    def block(digest: str) -> Any:
        if digest not in blocks:
            row = connection.execute(
                "SELECT body_json FROM snapshot_blocks WHERE digest=?",
                (digest,),
            ).fetchone()
            if row is None or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != digest:
                raise SessionConflict("A required context block is missing or corrupt")
            blocks[digest] = json.loads(row[0])
        return blocks[digest]

    return {
        key: [block(digest) for digest in reference["items"]]
        if "items" in reference
        else block(reference["value"])
        for key, reference in json.loads(manifest).items()
    }


def read_snapshot(
    connection: sqlite3.Connection, snapshot_id: str, *, session_id: str | None = None
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM context_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    if row is None or (session_id is not None and row["session_id"] != session_id):
        raise KeyError("Snapshot is missing or belongs to another session")
    if hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["content_hash"]:
        raise SessionConflict("Snapshot content hash mismatch")
    result = dict(row)
    result["payload"] = decode_context(connection, result.pop("payload_json"))
    return result
