"""Durable delivery claims and receipts, separate from execution qualification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from run_agent_coding.storage.sessions import canonical_json
from run_agent_core.session.contracts import SessionConflict
from run_agent_gateway.contracts import GatewayOwner
from run_agent_gateway.repository import GatewayRepository


@dataclass(frozen=True, slots=True)
class Delivery:
    delivery_id: str
    task_id: str | None
    kind: str
    destination: dict[str, Any]
    content: dict[str, Any]
    attempt: int


class OutboxRepository:
    def __init__(self, gateway: GatewayRepository) -> None:
        self.gateway = gateway

    async def claim(self, owner: GatewayOwner, *, limit: int = 16) -> list[Delivery]:
        if not 1 <= limit <= 64:
            raise ValueError("Delivery batch limit must be between 1 and 64")

        def claim(connection: sqlite3.Connection) -> list[Delivery]:
            self.gateway.assert_owner(connection, owner)
            rows = connection.execute(
                "SELECT o.* FROM gateway_outbox o "
                "WHERE o.status='pending' AND o.next_attempt_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM gateway_outbox p WHERE "
                "((p.task_id=o.task_id AND o.kind IN ('result','control')) OR "
                "(p.control_id=o.control_id AND o.kind='control')) "
                "AND p.kind='accepted' AND p.status IN ('pending','sending')) "
                "AND NOT EXISTS (SELECT 1 FROM gateway_outbox p WHERE "
                "p.task_id=o.task_id AND o.kind='result' AND p.kind='control' "
                "AND p.status IN ('pending','sending')) "
                "ORDER BY o.next_attempt_at,o.created_at,o.delivery_id LIMIT ?",
                (self.gateway.clock(), limit),
            ).fetchall()
            deliveries = []
            for row in rows:
                if hashlib.sha256(row["content_json"].encode()).hexdigest() != row["content_hash"]:
                    raise SessionConflict("Outbox content hash mismatch")
                connection.execute(
                    "UPDATE gateway_outbox SET status='sending',attempts=attempts+1,"
                    "claimed_by=?,claim_generation=? WHERE delivery_id=?",
                    (owner.owner_id, owner.generation, row["delivery_id"]),
                )
                deliveries.append(
                    Delivery(
                        row["delivery_id"],
                        row["task_id"],
                        row["kind"],
                        json.loads(row["destination_json"]),
                        json.loads(row["content_json"]),
                        row["attempts"] + 1,
                    )
                )
            return deliveries

        return await self.gateway.database.run(claim, write=True)

    def _claim(
        self, connection: sqlite3.Connection, owner: GatewayOwner, delivery: Delivery
    ) -> sqlite3.Row:
        self.gateway.assert_owner(connection, owner)
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM gateway_outbox WHERE delivery_id=?", (delivery.delivery_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != "sending"
            or row["claimed_by"] != owner.owner_id
            or row["claim_generation"] != owner.generation
            or row["attempts"] != delivery.attempt
        ):
            raise SessionConflict("Delivery claim is stale")
        return row

    async def acknowledge(
        self, owner: GatewayOwner, delivery: Delivery, receipt: dict[str, Any]
    ) -> None:
        encoded = canonical_json(receipt)
        if len(encoded.encode()) > 65536:
            raise ValueError("Channel receipt exceeds 64 KiB")

        def acknowledge(connection: sqlite3.Connection) -> None:
            self.gateway.assert_owner(connection, owner)
            previous = connection.execute(
                "SELECT * FROM gateway_outbox WHERE delivery_id=?", (delivery.delivery_id,)
            ).fetchone()
            if previous is not None and previous["status"] == "sent":
                if (previous["receipt_json"], previous["attempts"]) != (encoded, delivery.attempt):
                    raise SessionConflict("Delivery already has a different receipt")
                return
            self._claim(connection, owner, delivery)
            connection.execute(
                "UPDATE gateway_outbox SET status='sent',receipt_json=?,sent_at=?,error=NULL "
                "WHERE delivery_id=?",
                (encoded, self.gateway.clock(), delivery.delivery_id),
            )

        await self.gateway.database.run(acknowledge, write=True)

    async def fail(
        self,
        owner: GatewayOwner,
        delivery: Delivery,
        error: str,
        *,
        permanent: bool = False,
        max_attempts: int = 8,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("Delivery attempt limit must be positive")

        def fail(connection: sqlite3.Connection) -> None:
            self._claim(connection, owner, delivery)
            terminal = permanent or delivery.attempt >= max_attempts
            delay = min(300.0, 2.0 ** min(delivery.attempt - 1, 10))
            connection.execute(
                "UPDATE gateway_outbox SET status=?,error=?,next_attempt_at=?,claimed_by=NULL,"
                "claim_generation=NULL WHERE delivery_id=?",
                (
                    "failed" if terminal else "pending",
                    error[:4096],
                    self.gateway.clock() + delay,
                    delivery.delivery_id,
                ),
            )

        await self.gateway.database.run(fail, write=True)

    async def for_task(self, task_id: str, *, principal_id: str) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            self.gateway._task(connection, task_id, principal_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT delivery_id,kind,status,attempts,error,receipt_json,next_attempt_at "
                    "FROM gateway_outbox WHERE task_id=? ORDER BY created_at",
                    (task_id,),
                )
            ]

        return await self.gateway.database.run(read)
