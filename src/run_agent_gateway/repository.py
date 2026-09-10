"""Gateway admission, reservations and completion in the shared SQLite transaction."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from time import time
from typing import Any, cast
from uuid import uuid4

from run_agent_coding.storage.handle import OutcomeCommitter
from run_agent_coding.storage.sessions import SqliteSessionRepository, canonical_json
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import AssistantMessage
from run_agent_core.session.contracts import CompletionReceipt, RunOutcome, SessionConflict
from run_agent_core.session.entries import MessageEntry
from run_agent_gateway.contracts import (
    AdmissionReceipt,
    AdmissionRejected,
    Assignment,
    DuplicateConflict,
    GatewayLimits,
    GatewayOwner,
    GatewayOwnershipLost,
    RouteIdentity,
    Submission,
)
from run_agent_gateway.routing import route_key

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "outcome_unknown"})


class GatewayRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        limits: GatewayLimits | None = None,
        clock: Callable[[], float] = time,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.database, self.limits, self.clock = database, limits or GatewayLimits(), clock
        self.sessions = SqliteSessionRepository(database, clock=clock, fault=fault)
        self.fault = fault

    def _fault(self, point: str) -> None:
        if self.fault is not None:
            self.fault(point)

    async def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

        def initialize(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT value_json FROM host_metadata WHERE key='gateway.schema'"
            ).fetchone()
            if row is not None:
                if json.loads(row[0]) != {"version": 1}:
                    raise ValueError("Unsupported Gateway schema")
                return
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO host_metadata VALUES ('gateway.schema',?)",
                (canonical_json({"version": 1}),),
            )

        await self.database.run(initialize, write=True)

    async def acquire_owner(self, owner_id: str, *, lease_seconds: float = 30) -> GatewayOwner:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("Owner identity and positive lease are required")

        def acquire(connection: sqlite3.Connection) -> GatewayOwner:
            now = self.clock()
            previous = connection.execute("SELECT * FROM gateway_owner").fetchone()
            if previous and previous["active"] and previous["expires_at"] > now:
                raise GatewayOwnershipLost("Gateway already has a live owner")
            generation = previous["generation"] + 1 if previous else 1
            # Expiry is not proof that a tool exited. Retain its slot and quarantine
            # its workspace until recovery has verified the process outcome.
            orphaned = connection.execute(
                "SELECT a.run_id,t.session_id FROM gateway_attempts a "
                "JOIN gateway_tasks t ON t.task_id=a.task_id WHERE a.released=0"
            ).fetchall()
            for run in orphaned:
                connection.execute(
                    "UPDATE gateway_attempts SET status='outcome_unknown' WHERE run_id=?",
                    (run["run_id"],),
                )
                connection.execute(
                    "UPDATE gateway_tasks SET status='outcome_unknown',finished_at=?,"
                    "error='Previous runner must be reconciled' WHERE run_id=? "
                    "AND status IN ('running','cancelling')",
                    (now, run["run_id"]),
                )
                connection.execute(
                    "UPDATE gateway_workspaces SET status='quarantined',"
                    "reason='Previous runner must be reconciled' WHERE run_id=?",
                    (run["run_id"],),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO execution_revocations VALUES (?,?,?)",
                    (run["run_id"], "Gateway owner lost", now),
                )
                connection.execute(
                    "UPDATE executions SET status='outcome_unknown',finished_at=? "
                    "WHERE run_id=? AND status='running'",
                    (now, run["run_id"]),
                )
                connection.execute(
                    "UPDATE sessions SET owner_active=0,generation=generation+1 WHERE session_id=?",
                    (run["session_id"],),
                )
                connection.execute(
                    "UPDATE extension_owners SET active=0 WHERE session_id=?",
                    (run["session_id"],),
                )
            connection.execute(
                "INSERT INTO gateway_owner(singleton,owner_id,generation,expires_at,"
                "active,accepting) "
                "VALUES (1,?,?,?,1,1) ON CONFLICT(singleton) DO UPDATE SET "
                "owner_id=excluded.owner_id,generation=excluded.generation,"
                "expires_at=excluded.expires_at,active=1,accepting=1",
                (owner_id, generation, now + lease_seconds),
            )
            connection.execute(
                "UPDATE gateway_outbox SET status='pending',claimed_by=NULL,claim_generation=NULL "
                "WHERE status='sending'"
            )
            return GatewayOwner(owner_id, generation)

        return await self.database.run(acquire, write=True)

    def assert_owner(self, connection: sqlite3.Connection, owner: GatewayOwner) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute("SELECT * FROM gateway_owner").fetchone()
        if (
            row is None
            or not row["active"]
            or row["owner_id"] != owner.owner_id
            or row["generation"] != owner.generation
            or row["expires_at"] <= self.clock()
        ):
            raise GatewayOwnershipLost("Gateway ownership is no longer current")
        return row

    async def renew(self, owner: GatewayOwner, *, lease_seconds: float = 30) -> None:
        def renew(connection: sqlite3.Connection) -> None:
            self.assert_owner(connection, owner)
            connection.execute(
                "UPDATE gateway_owner SET expires_at=?", (self.clock() + lease_seconds,)
            )

        await self.database.run(renew, write=True)

    async def stop_accepting(self, owner: GatewayOwner) -> None:
        def stop(connection: sqlite3.Connection) -> None:
            self.assert_owner(connection, owner)
            connection.execute("UPDATE gateway_owner SET accepting=0")

        await self.database.run(stop, write=True)

    def _workspace(self, connection: sqlite3.Connection, path: Path) -> str:
        canonical = os.path.normcase(str(path.resolve()))
        identity = hashlib.sha256(canonical.encode()).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO gateway_workspaces(workspace_id,path,status) "
            "VALUES (?,?,'available')",
            (identity, canonical),
        )
        return identity

    def _route(
        self,
        connection: sqlite3.Connection,
        submission: Submission,
        *,
        model: str,
        provider_name: str | None,
    ) -> sqlite3.Row:
        key = route_key(submission.route)
        row = connection.execute(
            "SELECT * FROM gateway_routes WHERE route_key=?", (key,)
        ).fetchone()
        if row is None:
            session_id = uuid4().hex
            self.sessions.create_in_transaction(
                connection,
                session_id=session_id,
                principal_id=submission.principal_id,
                cwd=submission.workspace,
                model=model,
                provider_name=provider_name,
            )
            connection.execute(
                "INSERT INTO gateway_routes(route_key,principal_id,session_id,epoch,"
                "destination_json) "
                "VALUES (?,?,?,1,?)",
                (key, submission.principal_id, session_id, key),
            )
            row = connection.execute(
                "SELECT * FROM gateway_routes WHERE route_key=?", (key,)
            ).fetchone()
        if row["principal_id"] != submission.principal_id:
            raise PermissionError("Route belongs to a different authenticated principal")
        if row["pending_new"]:
            raise AdmissionRejected("Previous session is stopping before route replacement")
        return cast(sqlite3.Row, row)

    async def admit(
        self,
        owner: GatewayOwner,
        submission: Submission,
        *,
        model: str,
        provider_name: str | None = None,
    ) -> AdmissionReceipt:
        key = route_key(submission.route)
        if (
            not submission.principal_id
            or len(submission.principal_id.encode()) > 256
            or not submission.source_message_id
            or len(submission.source_message_id.encode()) > 256
            or not submission.content.strip()
            or submission.lane not in {"foreground", "background"}
        ):
            raise AdmissionRejected("Invalid identity, content or task category")
        payload = canonical_json(
            {
                "route": key,
                "principal": submission.principal_id,
                "content": submission.content,
                "workspace": str(submission.workspace.resolve()),
                "lane": submission.lane,
                "metadata": submission.metadata,
            }
        )
        if len(payload.encode()) > self.limits.payload_bytes:
            raise AdmissionRejected("Message exceeds the payload byte limit")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        metadata_json = canonical_json(json.loads(payload)["metadata"])
        task_id = uuid4().hex

        def admit(connection: sqlite3.Connection) -> AdmissionReceipt:
            owned = self.assert_owner(connection, owner)
            previous = connection.execute(
                "SELECT * FROM gateway_inbox WHERE adapter_instance_id=? AND source_message_id=?",
                (submission.route.adapter_instance_id, submission.source_message_id),
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise DuplicateConflict("Source message ID was reused with different content")
                return AdmissionReceipt(**json.loads(previous["receipt_json"]), duplicate=True)
            if not owned["accepting"]:
                raise AdmissionRejected("Gateway is stopping ordinary admission")
            route = self._route(connection, submission, model=model, provider_name=provider_name)
            origin = route["session_id"]
            counts = dict(
                connection.execute(
                    "SELECT lane,COUNT(*) FROM gateway_tasks WHERE status='queued' GROUP BY lane"
                ).fetchall()
            )
            total = sum(counts.values())
            opposite_reserved = (
                self.limits.waiting_background_reserved
                if submission.lane == "foreground"
                else self.limits.waiting_foreground_reserved
            )
            if (
                total >= self.limits.waiting_total
                or counts.get(submission.lane, 0) >= self.limits.waiting_total - opposite_reserved
            ):
                raise AdmissionRejected("Waiting capacity reserved for the other lane or full")
            per_session = connection.execute(
                "SELECT COUNT(*) FROM gateway_tasks WHERE origin_session_id=? AND status='queued'",
                (origin,),
            ).fetchone()[0]
            per_principal = connection.execute(
                "SELECT COUNT(*) FROM gateway_tasks WHERE principal_id=? AND status='queued'",
                (submission.principal_id,),
            ).fetchone()[0]
            if per_session >= self.limits.per_session or per_principal >= self.limits.per_principal:
                raise AdmissionRejected("Session or principal waiting limit reached")
            if submission.lane == "background":
                roots = connection.execute(
                    "SELECT COUNT(*) FROM gateway_tasks WHERE principal_id=? AND lane='background' "
                    "AND status IN ('queued','running','cancelling')",
                    (submission.principal_id,),
                ).fetchone()[0]
                if roots >= self.limits.background_roots_per_principal:
                    raise AdmissionRejected("Principal background root task limit reached")
            backlog = connection.execute(
                "SELECT COUNT(*) FROM gateway_outbox WHERE status IN ('pending','sending')"
            ).fetchone()[0]
            reserved_results = connection.execute(
                "SELECT COUNT(*) FROM gateway_tasks "
                "WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if backlog + reserved_results + 2 > self.limits.outbox_pending:
                raise AdmissionRejected("Delivery backlog is full")
            session_id = origin
            if submission.lane == "background":
                session_id = uuid4().hex
                self.sessions.create_in_transaction(
                    connection,
                    session_id=session_id,
                    principal_id=submission.principal_id,
                    cwd=submission.workspace,
                    model=model,
                    provider_name=provider_name,
                )
            workspace_id = self._workspace(connection, submission.workspace)
            source_head = connection.execute(
                "SELECT b.head_id FROM branches b JOIN sessions s "
                "ON b.session_id=s.session_id AND b.branch_id=s.active_branch_id "
                "WHERE s.session_id=?",
                (origin,),
            ).fetchone()[0]
            cursor = connection.execute(
                "INSERT INTO gateway_tasks(task_id,route_key,principal_id,session_id,"
                "origin_session_id,"
                "conversation_epoch,source_head_id,lane,workspace_id,content,metadata_json,"
                "destination_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?)",
                (
                    task_id,
                    key,
                    submission.principal_id,
                    session_id,
                    origin,
                    route["epoch"],
                    source_head,
                    submission.lane,
                    workspace_id,
                    submission.content,
                    metadata_json,
                    key,
                    self.clock(),
                ),
            )
            sequence = cast(int, cursor.lastrowid)
            self._fault("gateway_task_inserted")
            receipt = AdmissionReceipt(task_id, session_id, route["epoch"], sequence)
            body = asdict(receipt)
            body.pop("duplicate")
            connection.execute(
                "INSERT INTO gateway_inbox VALUES (?,?,?,?,?,?)",
                (
                    submission.route.adapter_instance_id,
                    submission.source_message_id,
                    digest,
                    task_id,
                    canonical_json(body),
                    self.clock(),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO gateway_session_order(session_id) VALUES (?)",
                (session_id,),
            )
            self._outbox(connection, task_id, "accepted", key, {"status": "accepted", **body})
            self._fault("gateway_admitted")
            return receipt

        return await self.database.run(admit, write=True)

    def _outbox(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        destination: str,
        content: dict[str, Any],
    ) -> str:
        frozen = canonical_json(content)
        digest = hashlib.sha256(frozen.encode()).hexdigest()
        previous = connection.execute(
            "SELECT delivery_id,content_hash FROM gateway_outbox WHERE task_id=? AND kind=?",
            (task_id, kind),
        ).fetchone()
        if previous:
            if previous["content_hash"] != digest:
                raise SessionConflict("Delivery identity has different content")
            return cast(str, previous["delivery_id"])
        delivery_id = uuid4().hex
        connection.execute(
            "INSERT INTO gateway_outbox(delivery_id,task_id,kind,destination_json,content_json,"
            "content_hash,status,next_attempt_at,created_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
            (delivery_id, task_id, kind, destination, frozen, digest, self.clock(), self.clock()),
        )
        return delivery_id

    async def claim_next(self, owner: GatewayOwner) -> Assignment | None:
        def claim(connection: sqlite3.Connection) -> Assignment | None:
            owned = self.assert_owner(connection, owner)
            counts = dict(
                connection.execute(
                    "SELECT t.lane,COUNT(*) FROM gateway_attempts a JOIN gateway_tasks t "
                    "ON a.task_id=t.task_id WHERE a.released=0 GROUP BY t.lane"
                ).fetchall()
            )
            if sum(counts.values()) >= self.limits.running_total:
                return None
            rows = connection.execute(
                "SELECT t.*,w.path FROM gateway_tasks t JOIN gateway_workspaces w "
                "ON t.workspace_id=w.workspace_id JOIN gateway_session_order s "
                "ON t.session_id=s.session_id WHERE t.status='queued' AND w.status='available' "
                "AND NOT EXISTS (SELECT 1 FROM gateway_tasks p WHERE p.session_id=t.session_id "
                "AND p.seq<t.seq AND p.status IN ('queued','blocked')) "
                "AND NOT EXISTS (SELECT 1 FROM gateway_attempts a JOIN gateway_tasks r "
                "ON a.task_id=r.task_id WHERE r.session_id=t.session_id AND a.released=0) "
                "ORDER BY s.last_dispatched,t.seq"
            ).fetchall()
            available: dict[str, sqlite3.Row] = {}
            for row in rows:
                available.setdefault(row["lane"], row)
            reservations = {
                "foreground": self.limits.running_foreground_reserved,
                "background": self.limits.running_background_reserved,
            }
            lanes = [
                owned["next_lane"],
                "background" if owned["next_lane"] == "foreground" else "foreground",
            ]
            lanes.sort(key=lambda lane: counts.get(lane, 0) >= reservations[lane])
            selected = next(
                (
                    available[lane]
                    for lane in lanes
                    if lane in available
                    and counts.get(lane, 0)
                    < self.limits.running_total
                    - reservations["background" if lane == "foreground" else "foreground"]
                ),
                None,
            )
            if selected is None:
                return None
            task_id, run_id = selected["task_id"], uuid4().hex
            attempt, generation = selected["attempt"] + 1, selected["generation"] + 1
            connection.execute(
                "UPDATE gateway_tasks SET status='running',run_id=?,attempt=?,generation=?,"
                "started_at=? WHERE task_id=?",
                (run_id, attempt, generation, self.clock(), task_id),
            )
            connection.execute(
                "INSERT INTO gateway_attempts(run_id,task_id,attempt,generation,owner_id,"
                "owner_generation,status,started_at) VALUES (?,?,?,?,?,?,'running',?)",
                (
                    run_id,
                    task_id,
                    attempt,
                    generation,
                    owner.owner_id,
                    owner.generation,
                    self.clock(),
                ),
            )
            connection.execute(
                "UPDATE gateway_workspaces SET status='leased',run_id=? WHERE workspace_id=?",
                (run_id, selected["workspace_id"]),
            )
            connection.execute(
                "UPDATE gateway_session_order SET last_dispatched=? WHERE session_id=?",
                (owned["dispatch_seq"] + 1, selected["session_id"]),
            )
            connection.execute(
                "UPDATE gateway_owner SET dispatch_seq=dispatch_seq+1,next_lane=?",
                ("background" if selected["lane"] == "foreground" else "foreground",),
            )
            self._fault("gateway_reserved")
            return Assignment(
                task_id,
                run_id,
                attempt,
                generation,
                selected["session_id"],
                selected["principal_id"],
                selected["lane"],
                selected["workspace_id"],
                Path(selected["path"]),
                selected["content"],
                json.loads(selected["metadata_json"]),
            )

        return await self.database.run(claim, write=True)

    def _assignment(
        self,
        connection: sqlite3.Connection,
        owner: GatewayOwner,
        assignment: Assignment,
        *,
        cancelling: bool = False,
    ) -> sqlite3.Row:
        self.assert_owner(connection, owner)
        row = connection.execute(
            "SELECT * FROM gateway_tasks WHERE task_id=?",
            (assignment.task_id,),
        ).fetchone()
        permitted_generations = {assignment.generation}
        if cancelling and row is not None and row["status"] in {"cancelling", "cancelled"}:
            permitted_generations.add(assignment.generation + 1)
        attempt = connection.execute(
            "SELECT * FROM gateway_attempts WHERE run_id=?",
            (assignment.run_id,),
        ).fetchone()
        if (
            row is None
            or row["run_id"] != assignment.run_id
            or row["generation"] not in permitted_generations
            or attempt is None
            or attempt["owner_id"] != owner.owner_id
            or attempt["owner_generation"] != owner.generation
            or (attempt["released"] and row["status"] not in TERMINAL)
        ):
            raise SessionConflict("Gateway run qualification is stale")
        return cast(sqlite3.Row, row)

    async def complete(
        self,
        owner: GatewayOwner,
        assignment: Assignment,
        *,
        status: str,
        output: str = "",
        error: str | None = None,
        outcome: RunOutcome | None = None,
    ) -> CompletionReceipt | None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("Invalid completion status")
        if outcome is not None:
            outcome = replace(
                outcome, entries=tuple(entry.model_copy(deep=True) for entry in outcome.entries)
            )

        def complete(connection: sqlite3.Connection) -> CompletionReceipt | None:
            task = self._assignment(connection, owner, assignment, cancelling=status == "cancelled")
            if task["status"] == "cancelling" and status != "cancelled":
                raise SessionConflict("Stop committed before this result")
            if task["status"] in TERMINAL and (task["status"], task["output"], task["error"]) != (
                status,
                output,
                error,
            ):
                raise SessionConflict("Task already has a different completion")
            receipt = None
            if outcome is not None:
                if (
                    outcome.token.run_id != assignment.run_id
                    or outcome.token.session_id != assignment.session_id
                    or outcome.status != status
                ):
                    raise SessionConflict("Outcome does not belong to the assigned task")
                receipt = self.sessions.complete_in_transaction(connection, outcome)
            elif connection.execute(
                "SELECT 1 FROM executions WHERE run_id=?", (assignment.run_id,)
            ).fetchone():
                raise SessionConflict("A Coding execution requires an atomic session outcome")
            if task["status"] in TERMINAL:
                if (
                    connection.execute(
                        "SELECT 1 FROM gateway_outbox WHERE task_id=? AND kind='result'",
                        (assignment.task_id,),
                    ).fetchone()
                    is None
                ):
                    raise SessionConflict("Committed task is missing its result delivery")
                return receipt
            connection.execute(
                "UPDATE gateway_tasks SET status=?,output=?,error=?,finished_at=? WHERE task_id=?",
                (status, output, error, self.clock(), assignment.task_id),
            )
            connection.execute(
                "UPDATE gateway_attempts SET status=?,finished_at=? WHERE run_id=?",
                (status, self.clock(), assignment.run_id),
            )
            self._fault("gateway_task_completed")
            self._outbox(
                connection,
                assignment.task_id,
                "result",
                task["destination_json"],
                {
                    "task_id": assignment.task_id,
                    "run_id": assignment.run_id,
                    "status": status,
                    "session_id": assignment.session_id,
                    "conversation_epoch": task["conversation_epoch"],
                    "output": output,
                    "error": error,
                },
            )
            self._fault("gateway_outbox_inserted")
            return receipt

        return await self.database.run(complete, write=True)

    def committer(self, owner: GatewayOwner, assignment: Assignment) -> OutcomeCommitter:
        """Bind the Coding completion seam to this durable attempt and its Outbox."""

        async def commit(outcome: RunOutcome) -> CompletionReceipt:
            final = next(
                (
                    entry.message
                    for entry in reversed(outcome.entries)
                    if isinstance(entry, MessageEntry)
                    and isinstance(entry.message, AssistantMessage)
                ),
                None,
            )
            receipt = await self.complete(
                owner,
                assignment,
                status=outcome.status,
                output=final.text if final is not None else "",
                error=outcome.error,
                outcome=outcome,
            )
            assert receipt is not None
            return receipt

        return commit

    async def release(self, owner: GatewayOwner, assignment: Assignment) -> None:
        """The caller has observed the Runner and all its owned work actually exit."""

        def release(connection: sqlite3.Connection) -> None:
            self.assert_owner(connection, owner)
            row = connection.execute(
                "SELECT * FROM gateway_attempts WHERE run_id=?",
                (assignment.run_id,),
            ).fetchone()
            if row is None or (row["owner_id"], row["owner_generation"]) != (
                owner.owner_id,
                owner.generation,
            ):
                raise SessionConflict("Cannot release another owner's execution slot")
            if row["status"] not in {"succeeded", "failed", "cancelled"}:
                raise SessionConflict("Runner has no confirmed terminal outcome")
            connection.execute(
                "UPDATE gateway_attempts SET released=1 WHERE run_id=?",
                (assignment.run_id,),
            )
            connection.execute(
                "UPDATE gateway_workspaces SET status='available',run_id=NULL WHERE run_id=? "
                "AND status='leased'",
                (assignment.run_id,),
            )

        await self.database.run(release, write=True)

    def _cancel(self, connection: sqlite3.Connection, task: sqlite3.Row) -> str:
        if task["status"] == "queued":
            connection.execute(
                "UPDATE gateway_tasks SET status='cancelled',error='Cancelled before execution',"
                "finished_at=? WHERE task_id=?",
                (self.clock(), task["task_id"]),
            )
            self._outbox(
                connection,
                task["task_id"],
                "result",
                task["destination_json"],
                {
                    "task_id": task["task_id"],
                    "status": "cancelled",
                    "session_id": task["session_id"],
                    "conversation_epoch": task["conversation_epoch"],
                    "output": "",
                    "error": "Cancelled before execution",
                },
            )
            return "cancelled"
        if task["status"] == "running":
            connection.execute(
                "UPDATE gateway_tasks SET status='cancelling',generation=generation+1 "
                "WHERE task_id=?",
                (task["task_id"],),
            )
            connection.execute(
                "UPDATE gateway_attempts SET status='cancelling' WHERE run_id=?",
                (task["run_id"],),
            )
            connection.execute(
                "INSERT OR IGNORE INTO execution_revocations VALUES (?,?,?)",
                (task["run_id"], "Gateway stop requested", self.clock()),
            )
            return "cancelling"
        return cast(str, task["status"])

    async def cancel(self, owner: GatewayOwner, task_id: str, *, principal_id: str) -> str:
        def cancel(connection: sqlite3.Connection) -> str:
            self.assert_owner(connection, owner)
            task = self._task(connection, task_id, principal_id)
            return self._cancel(connection, task)

        return await self.database.run(cancel, write=True)

    async def stop(
        self, owner: GatewayOwner, route: RouteIdentity, *, principal_id: str
    ) -> dict[str, str]:
        def stop(connection: sqlite3.Connection) -> dict[str, str]:
            self.assert_owner(connection, owner)
            binding = connection.execute(
                "SELECT * FROM gateway_routes WHERE route_key=?",
                (route_key(route),),
            ).fetchone()
            if binding is None:
                return {}
            if binding["principal_id"] != principal_id:
                raise PermissionError("Route belongs to another principal")
            rows = connection.execute(
                "SELECT * FROM gateway_tasks WHERE session_id=? AND lane='foreground' "
                "AND status IN ('queued','running','cancelling')",
                (binding["session_id"],),
            ).fetchall()
            return {row["task_id"]: self._cancel(connection, row) for row in rows}

        return await self.database.run(stop, write=True)

    @staticmethod
    def _task(connection: sqlite3.Connection, task_id: str, principal_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM gateway_tasks WHERE task_id=? AND principal_id=?",
            (task_id, principal_id),
        ).fetchone()
        if row is None:
            raise KeyError("Task is missing or not visible to this principal")
        return cast(sqlite3.Row, row)

    async def task(self, task_id: str, *, principal_id: str) -> dict[str, Any]:
        return await self.database.run(
            lambda connection: dict(self._task(connection, task_id, principal_id))
        )

    async def release_owner(self, owner: GatewayOwner) -> None:
        def release(connection: sqlite3.Connection) -> None:
            self.assert_owner(connection, owner)
            if connection.execute(
                "SELECT 1 FROM gateway_attempts WHERE released=0 LIMIT 1"
            ).fetchone():
                raise SessionConflict("Gateway still owns unreleased runners")
            connection.execute("UPDATE gateway_owner SET active=0,accepting=0")

        await self.database.run(release, write=True)
