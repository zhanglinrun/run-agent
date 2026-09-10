"""Short durable control transactions, independent of long Coding prompts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from run_agent_coding.storage.sessions import canonical_json
from run_agent_gateway.contracts import (
    AdmissionRejected,
    DuplicateConflict,
    GatewayOwner,
    Submission,
)
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.routing import route_key

CONTROL_COMMANDS = frozenset({"/status", "/tasks", "/stop", "/cancel", "/new", "/steer"})


class SessionController:
    def __init__(self, repository: GatewayRepository) -> None:
        self.repository = repository

    async def handle(
        self,
        owner: GatewayOwner,
        submission: Submission,
        *,
        model: str,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        command, _, argument = submission.content.strip().partition(" ")
        argument = argument.strip()
        if command not in CONTROL_COMMANDS:
            raise ValueError("Unknown Gateway control")
        if command == "/steer":
            if not argument:
                raise ValueError("Usage: /steer <content>")
            receipt = await self.repository.admit(
                owner,
                replace(submission, content=argument, mode="steer"),
                model=model,
                provider_name=provider_name,
            )
            return {"status": "accepted", **asdict(receipt)}
        if command in {"/stop", "/new", "/tasks"} and argument:
            raise ValueError(f"{command} does not accept arguments")
        if command == "/cancel" and not argument:
            raise ValueError("Usage: /cancel <task_id>")
        if not submission.principal_id or not submission.source_message_id:
            raise ValueError("Control requires authenticated identity and source message ID")
        payload = canonical_json({**asdict(submission), "workspace": str(submission.workspace)})
        if len(payload.encode()) > 16384:
            raise AdmissionRejected("Control exceeds 16 KiB")
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = route_key(submission.route)
        destination = canonical_json(
            {**asdict(submission.route), "source_message_id": submission.source_message_id}
        )

        def control(connection: sqlite3.Connection) -> dict[str, Any]:
            self.repository.assert_owner(connection, owner)
            previous = connection.execute(
                "SELECT * FROM gateway_inbox WHERE adapter_instance_id=? AND source_message_id=?",
                (submission.route.adapter_instance_id, submission.source_message_id),
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest or previous["control_id"] is None:
                    raise DuplicateConflict("Source message ID was reused with different content")
                return {**json.loads(previous["receipt_json"]), "duplicate": True}
            pending = connection.execute(
                "SELECT COUNT(*) FROM gateway_outbox WHERE control_id IS NOT NULL "
                "AND status IN ('pending','sending')"
            ).fetchone()[0]
            pending += connection.execute(
                "SELECT COUNT(*) FROM gateway_controls WHERE state='waiting'"
            ).fetchone()[0]
            if pending + 2 > 128:
                raise AdmissionRejected("Control delivery capacity is full")
            binding = connection.execute(
                "SELECT * FROM gateway_routes WHERE route_key=?", (key,)
            ).fetchone()
            if binding is not None and binding["principal_id"] != submission.principal_id:
                raise PermissionError("Route belongs to another principal")
            control_id = uuid4().hex
            response: dict[str, Any] = {"control_id": control_id, "command": command}
            waiting = False
            if command in {"/status", "/tasks"}:
                if argument:
                    rows = [self.repository._task(connection, argument, submission.principal_id)]
                else:
                    task_rows = connection.execute(
                        "SELECT task_id FROM gateway_tasks WHERE principal_id=? "
                        "ORDER BY seq DESC LIMIT 32",
                        (submission.principal_id,),
                    ).fetchall()
                    rows = [
                        self.repository._task(connection, row["task_id"], submission.principal_id)
                        for row in task_rows
                    ]
                response.update(
                    status="status",
                    tasks=[
                        {
                            field: row[field]
                            for field in (
                                "task_id",
                                "session_id",
                                "lane",
                                "status",
                                "attempt",
                                "error",
                                "execution_status",
                                "released",
                                "workspace_status",
                                "workspace_error",
                                "target_run_id",
                                "consumed_entry_id",
                                "origin_session_id",
                                "source_head_id",
                                "source_watermark",
                                "resource_entry_id",
                                "revision_json",
                                "artifacts_json",
                            )
                        }
                        for row in rows
                    ],
                    session_id=binding["session_id"] if binding else None,
                )
            elif command == "/cancel":
                task = self.repository._task(connection, argument, submission.principal_id)
                response.update(status=self.repository._cancel(connection, task), task_id=argument)
                waiting = response["status"] == "cancelling"
            else:
                if command == "/new" and binding is None:
                    binding = self.repository._route(
                        connection, submission, model=model, provider_name=provider_name
                    )
                cancelled: dict[str, str] = {}
                if binding is not None:
                    connection.execute(
                        "UPDATE gateway_routes SET control_generation=control_generation+1 "
                        "WHERE route_key=?",
                        (key,),
                    )
                    rows = connection.execute(
                        "SELECT * FROM gateway_tasks WHERE session_id=? AND lane='foreground' "
                        "AND status IN ('queued','steering','running','cancelling')",
                        (binding["session_id"],),
                    ).fetchall()
                    cancelled = {
                        row["task_id"]: self.repository._cancel(connection, row) for row in rows
                    }
                response.update(
                    status="stopping" if "cancelling" in cancelled.values() else "stopped",
                    tasks=cancelled,
                )
                waiting = response["status"] == "stopping"
                if command == "/new":
                    assert binding is not None
                    if binding["pending_new"]:
                        raise AdmissionRejected("A new-session transition is already pending")
                    connection.execute(
                        "UPDATE gateway_routes SET pending_new=1 WHERE route_key=?", (key,)
                    )
                    response["status"] = "stopping"
                    waiting = True
            connection.execute(
                "INSERT INTO gateway_controls VALUES (?,?,?,?,?,?,?)",
                (
                    control_id,
                    submission.principal_id,
                    key,
                    command,
                    "waiting" if waiting else "completed",
                    canonical_json(response),
                    self.repository.clock(),
                ),
            )
            connection.execute(
                "INSERT INTO gateway_inbox(adapter_instance_id,source_message_id,payload_hash,"
                "control_id,receipt_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    submission.route.adapter_instance_id,
                    submission.source_message_id,
                    digest,
                    control_id,
                    canonical_json(response),
                    self.repository.clock(),
                ),
            )
            self.repository._outbox(
                connection,
                None,
                "accepted" if waiting else "control",
                destination,
                response,
                control_id=control_id,
            )
            return response

        return await self.repository.database.run(control, write=True)

    async def reconcile(self, owner: GatewayOwner) -> None:
        """Complete /new only after every old foreground reservation has been released."""

        def reconcile(connection: sqlite3.Connection) -> None:
            self.repository.assert_owner(connection, owner)
            controls = connection.execute(
                "SELECT c.*,o.destination_json FROM gateway_controls c JOIN gateway_outbox o "
                "ON c.control_id=o.control_id AND o.kind='accepted' "
                "WHERE c.state='waiting' AND c.command IN ('/stop','/cancel')"
            ).fetchall()
            for control in controls:
                response = json.loads(control["response_json"])
                task_ids = (
                    [response["task_id"]]
                    if control["command"] == "/cancel"
                    else list(response["tasks"])
                )
                if any(
                    connection.execute(
                        "SELECT 1 FROM gateway_attempts WHERE task_id=? AND released=0", (task_id,)
                    ).fetchone()
                    for task_id in task_ids
                ):
                    continue
                response["status"] = "cancelled" if control["command"] == "/cancel" else "stopped"
                if control["command"] == "/stop":
                    response["tasks"] = {
                        task_id: self.repository._task(
                            connection, task_id, control["principal_id"]
                        )["status"]
                        for task_id in task_ids
                    }
                connection.execute(
                    "UPDATE gateway_controls SET state='completed',response_json=? "
                    "WHERE control_id=?",
                    (canonical_json(response), control["control_id"]),
                )
                self.repository._outbox(
                    connection,
                    None,
                    "control",
                    control["destination_json"],
                    response,
                    control_id=control["control_id"],
                )
            rows = connection.execute(
                "SELECT c.control_id,o.destination_json AS control_destination,r.* "
                "FROM gateway_controls c JOIN gateway_routes r ON c.route_key=r.route_key "
                "JOIN gateway_outbox o ON o.control_id=c.control_id AND o.kind='accepted' "
                "WHERE c.command='/new' AND c.state='waiting' AND r.preparing=0 "
                "AND NOT EXISTS (SELECT 1 FROM gateway_attempts a JOIN gateway_tasks t "
                "ON a.task_id=t.task_id WHERE t.session_id=r.session_id "
                "AND t.lane='foreground' AND a.released=0)"
            ).fetchall()
            for row in rows:
                record = self.repository.sessions._record(connection, row["session_id"])
                session_id = uuid4().hex
                self.repository.sessions.create_in_transaction(
                    connection,
                    session_id=session_id,
                    principal_id=record.principal_id,
                    cwd=Path(record.cwd),
                    model=record.model,
                    provider_name=record.provider_name,
                    project_id=record.project_id,
                )
                connection.execute(
                    "UPDATE gateway_routes SET session_id=?,epoch=epoch+1,pending_new=0 "
                    "WHERE route_key=?",
                    (session_id, row["route_key"]),
                )
                response = {
                    "command": "/new",
                    "control_id": row["control_id"],
                    "status": "new_session",
                    "session_id": session_id,
                    "conversation_epoch": row["epoch"] + 1,
                }
                connection.execute(
                    "UPDATE gateway_controls SET state='completed',response_json=? "
                    "WHERE control_id=?",
                    (canonical_json(response), row["control_id"]),
                )
                self.repository._outbox(
                    connection,
                    None,
                    "control",
                    row["control_destination"],
                    response,
                    control_id=row["control_id"],
                )

        await self.repository.database.run(reconcile, write=True)
