"""Inspect orphaned attempts and release only verified, explicitly reviewed workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from run_agent_coding.host.process_identity import machine_identity, process_identity
from run_agent_coding.host.process_probe import inspect_native_process, terminate_orphan
from run_agent_coding.storage.sessions import canonical_json
from run_agent_core.session.contracts import SessionConflict
from run_agent_gateway.contracts import GatewayOwner
from run_agent_gateway.repository import GatewayRepository


class GatewayRecovery:
    def __init__(self, repository: GatewayRepository, owner: GatewayOwner | None = None) -> None:
        self.repository, self.owner = repository, owner

    def _assert_owner(self, connection: sqlite3.Connection) -> None:
        if self.owner is None:
            raise SessionConflict("Recovery changes require Gateway ownership")
        self.repository.assert_owner(connection, self.owner)

    async def _snapshot(self, run_id: str) -> dict[str, Any]:
        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            if self.owner is not None:
                self._assert_owner(connection)
            row = connection.execute(
                "SELECT a.*,t.session_id,t.principal_id,t.status AS task_status,"
                "t.destination_json,t.conversation_epoch,t.origin_session_id,t.lane,"
                "t.workspace_id,w.path,w.status AS workspace_status,h.process_json "
                "FROM gateway_attempts a JOIN gateway_tasks t ON a.task_id=t.task_id "
                "JOIN gateway_workspaces w ON t.workspace_id=w.workspace_id "
                "LEFT JOIN gateway_hosts h ON h.owner_id=a.owner_id "
                "AND h.generation=a.owner_generation WHERE a.run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Unknown Gateway execution")
            processes = [dict(p) for p in connection.execute(
                "SELECT * FROM managed_processes WHERE session_id=? ORDER BY process_id",
                (row["session_id"],),
            ).fetchall()]
            return {"attempt": dict(row), "processes": processes}

        return await self.repository.database.run(read)

    @staticmethod
    def _fingerprint(snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()

    async def inspect(self, run_id: str) -> dict[str, Any]:
        snapshot = await self._snapshot(run_id)
        task = snapshot["attempt"]
        checks: list[dict[str, Any]] = []
        host_dead = False
        try:
            native = json.loads(task["process_json"]) if task["process_json"] else None
            if native is not None and native.get("machine_identity") == machine_identity():
                host_dead = process_identity(native["pid"]) != native["identity"]
        except (OSError, KeyError, TypeError, ValueError):
            pass
        if host_dead:
            for row in snapshot["processes"]:
                if row["status"] in {"exited", "launch_failed"}:
                    check = {"empty": True, "reason": "Durable process completion"}
                else:
                    check = await asyncio.to_thread(
                        inspect_native_process, json.loads(row["intent_json"]),
                        json.loads(row["native_json"]) if row["native_json"] else None,
                    )
                checks.append({"process_id": row["process_id"], **check})
        report = {
            "run_id": run_id, "task_id": task["task_id"], "task_status": task["task_status"],
            "session_id": task["session_id"], "workspace": task["path"],
            "previous_host_exited": host_dead, "processes": checks,
            "processes_empty": host_dead and all(p["empty"] for p in checks),
            "fingerprint": self._fingerprint(snapshot),
            "review_required": task["task_status"] not in {"succeeded", "failed", "cancelled"},
            "released": bool(task["released"]),
        }
        report["workspace_available"] = Path(task["path"]).is_dir()
        return report

    async def inspect_all(self) -> list[dict[str, Any]]:
        ids = await self.repository.database.run(lambda c: [r[0] for r in c.execute(
            "SELECT run_id FROM gateway_attempts WHERE released=0 ORDER BY started_at"
        ).fetchall()])
        return [await self.inspect(run_id) for run_id in ids]

    async def reconcile(self) -> list[dict[str, Any]]:
        reports = await self.inspect_all()
        for report in reports:
            if report["processes_empty"] and not report["review_required"]:
                await self.release(
                    report["run_id"], note="Committed result; native cleanup verified"
                )
            else:
                await self._save_report(report)
        return reports

    async def terminate(self, run_id: str) -> dict[str, Any]:
        if self.owner is None:
            raise SessionConflict("Process termination requires exclusive Gateway ownership")
        report = await self.inspect(run_id)
        if not report["previous_host_exited"]:
            raise SessionConflict("Previous host process has not exited")
        snapshot = await self._snapshot(run_id)
        if self._fingerprint(snapshot) != report["fingerprint"]:
            raise SessionConflict("Execution changed during recovery inspection")
        for row in snapshot["processes"]:
            if row["status"] in {"launching", "running"}:
                await asyncio.to_thread(
                    terminate_orphan, json.loads(row["intent_json"]),
                    json.loads(row["native_json"]) if row["native_json"] else None,
                )
        result = await self.inspect(run_id)
        await self._save_report(result)
        return result

    async def _save_report(self, report: dict[str, Any]) -> None:
        def save(connection: sqlite3.Connection) -> None:
            self._assert_owner(connection)
            connection.execute(
                "INSERT INTO gateway_recovery(run_id,report_json,checked_at) VALUES (?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET report_json=excluded.report_json,"
                "checked_at=excluded.checked_at",
                (report["run_id"], canonical_json(report), self.repository.clock()),
            )
        await self.repository.database.run(save, write=True)

    async def release(self, run_id: str, *, note: str) -> dict[str, Any]:
        if not note.strip() or len(note.encode()) > 4096:
            raise ValueError("Recovery requires a review note of at most 4096 bytes")
        report = await self.inspect(run_id)
        if report["released"]:
            return report
        if not report["processes_empty"]:
            raise SessionConflict("Previous host or command processes are not verified stopped")
        if not report["workspace_available"]:
            raise SessionConflict("Workspace is unavailable; reconcile its location first")
        snapshot = await self._snapshot(run_id)
        if self._fingerprint(snapshot) != report["fingerprint"]:
            raise SessionConflict("Execution changed during recovery inspection")
        task = snapshot["attempt"]

        def release(connection: sqlite3.Connection) -> None:
            self._assert_owner(connection)
            current = connection.execute(
                "SELECT released,status FROM gateway_attempts WHERE run_id=?", (run_id,)
            ).fetchone()
            if current["released"] or current["status"] != task["status"]:
                raise SessionConflict("Execution changed before recovery commit")
            current_workspace = connection.execute(
                "SELECT run_id,status FROM gateway_workspaces WHERE workspace_id=?",
                (task["workspace_id"],),
            ).fetchone()
            if tuple(current_workspace) != (run_id, "quarantined"):
                raise SessionConflict("Workspace is not quarantined for this execution")
            session_owner = connection.execute(
                "SELECT owner_id,owner_active FROM sessions WHERE session_id=?",
                (task["session_id"],),
            ).fetchone()
            if session_owner["owner_active"] and session_owner["owner_id"] != task["owner_id"]:
                raise SessionConflict("Another writer has reopened this session")
            rows = [dict(p) for p in connection.execute(
                "SELECT * FROM managed_processes WHERE session_id=? ORDER BY process_id",
                (task["session_id"],),
            ).fetchall()]
            if rows != snapshot["processes"]:
                raise SessionConflict("Process journal changed during recovery inspection")
            now = self.repository.clock()
            for row in rows:
                if row["status"] in {"launching", "running"}:
                    connection.execute(
                        "UPDATE managed_processes SET status='exited',outcome_json=?,updated_at=? "
                        "WHERE process_id=?", (canonical_json({
                         "phase": "reconciled", "events": ["empty"],
                         "recovery_run_id": run_id, "effect": "unknown"}), now, row["process_id"]),
                    )
            connection.execute(
                "UPDATE sessions SET owner_active=0,generation=generation+1,recovery_required=0 "
                "WHERE session_id=?",
                (task["session_id"],),
            )
            connection.execute("UPDATE extension_owners SET active=0 WHERE session_id=?",
                               (task["session_id"],))
            connection.execute(
                "UPDATE gateway_attempts SET released=1,status=? WHERE run_id=?",
                (task["task_status"], run_id),
            )
            connection.execute(
                "UPDATE gateway_workspaces SET status='available',run_id=NULL,reason=NULL "
                "WHERE workspace_id=? AND run_id=?", (task["workspace_id"], run_id),
            )
            connection.execute(
                "INSERT INTO gateway_recovery VALUES (?,?,?,?,?,1) ON CONFLICT(run_id) DO UPDATE "
                "SET report_json=excluded.report_json,checked_at=excluded.checked_at,"
                "reviewed_at=excluded.reviewed_at,review_note=excluded.review_note,resolved=1",
                (run_id, canonical_json(report), now, now, note.strip()),
            )
            if report["review_required"]:
                self.repository._outbox(
                    connection, task["task_id"], "result", task["destination_json"], {
                    "task_id": task["task_id"], "run_id": run_id, "status": "outcome_unknown",
                    "session_id": task["session_id"],
                    "origin_session_id": task["origin_session_id"],
                    "conversation_epoch": task["conversation_epoch"], "lane": task["lane"],
                    "error": "Execution interrupted; external effects require review",
                    "workspace_released": True,
                })
            self.repository._fault("gateway_recovery_committed")

        await self.repository.database.run(release, write=True)
        return {**report, "released": True, "review_note": note.strip()}


__all__ = ["GatewayRecovery"]
