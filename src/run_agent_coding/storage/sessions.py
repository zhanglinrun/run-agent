"""Session history, branch heads and execution fences committed together."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from run_agent_coding.storage.snapshots import encode_context, read_snapshot, store_blocks
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.session.contracts import (
    AppendReceipt,
    BranchHead,
    CompletionReceipt,
    EntryPage,
    RunOutcome,
    RunToken,
    SessionConflict,
    StaleRunToken,
)
from run_agent_core.session.entries import SessionEntry, SessionInfoEntry

ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def entry_body(entry: SessionEntry) -> str:
    return canonical_json(entry.model_dump(mode="json", by_alias=True, exclude={"seq"}))


def decode_entry(row: sqlite3.Row) -> SessionEntry:
    data = json.loads(row["body_json"])
    data["seq"] = row["seq"]
    return ENTRY_ADAPTER.validate_python(data)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    principal_id: str
    project_id: str
    cwd: str
    model: str
    provider_name: str | None
    title: str | None
    created_at: float
    updated_at: float
    metadata: dict[str, Any]


class SqliteSessionRepository:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        clock: Callable[[], float] = time,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.clock = clock
        self.fault = fault

    async def create_session(
        self,
        *,
        cwd: str | Path,
        principal_id: str,
        model: str,
        session_id: str | None = None,
        project_id: str | None = None,
        provider_name: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        identity = session_id or uuid4().hex
        if not identity or not principal_id:
            raise ValueError("Session and principal identities must not be empty")
        resolved_cwd = str(Path(cwd).resolve())
        metadata_json = canonical_json(metadata or {})

        def create(connection: sqlite3.Connection) -> SessionRecord:
            return self.create_in_transaction(
                connection,
                session_id=identity,
                principal_id=principal_id,
                cwd=Path(resolved_cwd),
                model=model,
                provider_name=provider_name,
                project_id=project_id,
                title=title,
                metadata=json.loads(metadata_json),
            )

        return await self.database.run(create, write=True)

    def create_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        principal_id: str,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        project_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        """Create the initial session and branch inside a host admission transaction."""
        canonical_path = str(cwd.resolve())
        now = self.clock()
        if not session_id or not principal_id:
            raise ValueError("Session and principal identities must not be empty")
        if project_id is not None:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"Unknown project: {project_id}")
        else:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE canonical_path=?", (canonical_path,)
            ).fetchone()
            project_id = project[0] if project else uuid4().hex
            if project is None:
                connection.execute(
                    "INSERT INTO projects VALUES (?,?,?)", (project_id, canonical_path, now)
                )
        try:
            connection.execute(
                "INSERT INTO sessions(session_id,principal_id,project_id,cwd,model,provider_name,"
                "title,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    principal_id,
                    project_id,
                    canonical_path,
                    model,
                    provider_name,
                    title,
                    canonical_json(metadata or {}),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SessionConflict(f"Session already exists: {session_id}") from exc
        connection.execute(
            "INSERT INTO branches(session_id,branch_id,created_at) VALUES (?,'main',?)",
            (session_id, now),
        )
        return self._record(connection, session_id)

    @staticmethod
    def _record(connection: sqlite3.Connection, session_id: str) -> SessionRecord:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        return SessionRecord(
            session_id=row["session_id"],
            principal_id=row["principal_id"],
            project_id=row["project_id"],
            cwd=row["cwd"],
            model=row["model"],
            provider_name=row["provider_name"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    async def get_session(self, session_id: str) -> SessionRecord:
        return await self.database.run(lambda connection: self._record(connection, session_id))

    def clone_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        origin_session_id: str,
        session_id: str,
        cwd: Path,
        principal_id: str,
    ) -> tuple[SessionRecord, str | None, int, str | None]:
        """Freeze the active source ancestry into an independent session in this transaction."""
        origin = self._record(connection, origin_session_id)
        if origin.principal_id != principal_id:
            raise PermissionError("Cannot clone another principal's session")
        source = connection.execute(
            "SELECT b.head_id,s.last_seq FROM sessions s JOIN branches b "
            "ON b.session_id=s.session_id AND b.branch_id=s.active_branch_id WHERE s.session_id=?",
            (origin_session_id,),
        ).fetchone()
        head, watermark = source["head_id"], source["last_seq"]
        self.create_in_transaction(
            connection,
            session_id=session_id,
            principal_id=principal_id,
            cwd=cwd,
            model=origin.model,
            provider_name=origin.provider_name,
            project_id=origin.project_id,
        )
        connection.execute(
            "WITH RECURSIVE ancestry(entry_id,parent_id,seq) AS ("
            "SELECT entry_id,parent_id,seq FROM entries WHERE session_id=? AND entry_id=? "
            "UNION ALL SELECT e.entry_id,e.parent_id,e.seq FROM entries e JOIN ancestry a "
            "ON e.entry_id=a.parent_id WHERE e.session_id=? AND e.seq<a.seq) "
            "INSERT INTO entries(session_id,entry_id,seq,parent_id,origin_branch_id,"
            "run_id,kind,body_json) "
            "SELECT ?,e.entry_id,e.seq,e.parent_id,'main',e.run_id,e.kind,e.body_json "
            "FROM entries e JOIN ancestry a ON e.entry_id=a.entry_id "
            "WHERE e.session_id=? ORDER BY e.seq",
            (origin_session_id, head, origin_session_id, session_id, origin_session_id),
        )
        resource = connection.execute(
            "SELECT entry_id FROM entries WHERE session_id=? AND kind='custom' "
            "AND json_extract(body_json,'$.namespace')='run.resources' ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        resource_id = resource["entry_id"] if resource is not None else None
        metadata = {
            **origin.metadata,
            "origin_session_id": origin_session_id,
            "source_head_id": head,
            "source_watermark": watermark,
            "source_resource_entry_id": resource_id,
        }
        # A new info entry records the new workspace without rewriting the source events.
        info = SessionInfoEntry(parent_id=head, cwd=str(cwd), title=None)
        sequence = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM entries WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                info.id,
                sequence,
                head,
                "main",
                f"fork-{session_id}",
                info.type,
                entry_body(info),
            ),
        )
        connection.execute(
            "UPDATE branches SET head_id=? WHERE session_id=? AND branch_id='main'",
            (info.id, session_id),
        )
        connection.execute(
            "UPDATE sessions SET last_seq=?,metadata_json=? WHERE session_id=?",
            (sequence, canonical_json(metadata), session_id),
        )
        if self.fault:
            self.fault("session_cloned")
        return self._record(connection, session_id), head, watermark, resource_id

    async def update_metadata(
        self,
        token: RunToken,
        *,
        model: str,
        provider_name: str | None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord:
        frozen = canonical_json(metadata) if metadata is not None else None

        def update(connection: sqlite3.Connection) -> SessionRecord:
            self.assert_token(connection, token)
            connection.execute(
                """UPDATE sessions SET model=?, provider_name=?, title=COALESCE(?,title),
                   metadata_json=COALESCE(?,metadata_json), updated_at=? WHERE session_id=?""",
                (model, provider_name, title, frozen, self.clock(), token.session_id),
            )
            return self._record(connection, token.session_id)

        return await self.database.run(update, write=True)

    async def begin_run(self, token: RunToken, *, branch_id: str, run_id: str) -> RunToken:
        def begin(connection: sqlite3.Connection) -> RunToken:
            self.assert_token(connection, token)
            previous = connection.execute(
                "SELECT status FROM executions WHERE run_id=?", (token.run_id,)
            ).fetchone()
            if previous is not None and previous[0] == "running":
                raise SessionConflict("The previous run has no committed outcome")
            self.head_in_transaction(connection, token.session_id, branch_id)
            if connection.execute(
                "SELECT 1 FROM execution_revocations WHERE run_id=?", (run_id,)
            ).fetchone():
                raise StaleRunToken("Run was cancelled before it began")
            new = RunToken(token.session_id, token.owner_id, run_id, token.generation + 1)
            connection.execute(
                "UPDATE sessions SET active_run_id=?, generation=? WHERE session_id=?",
                (run_id, new.generation, token.session_id),
            )
            connection.execute(
                """INSERT INTO executions(run_id,session_id,branch_id,owner_id,
                   generation,status,started_at)
                   VALUES (?,?,?,?,?,'running',?)""",
                (run_id, token.session_id, branch_id, token.owner_id, new.generation, self.clock()),
            )
            return new

        return await self.database.run(begin, write=True)

    def complete_in_transaction(
        self, connection: sqlite3.Connection, outcome: RunOutcome
    ) -> CompletionReceipt:
        row = connection.execute(
            "SELECT * FROM executions WHERE run_id=? AND session_id=?",
            (outcome.token.run_id, outcome.token.session_id),
        ).fetchone()
        if (
            row is None
            or row["generation"] != outcome.token.generation
            or row["branch_id"] != outcome.branch_id
            or row["owner_id"] != outcome.token.owner_id
        ):
            raise SessionConflict("Run attempt does not match its completion")
        encoded = canonical_json(
            {
                "status": outcome.status,
                "expected_head": outcome.expected_head,
                "entries": [
                    entry.model_dump(mode="json", exclude={"seq"}) for entry in outcome.entries
                ],
                "error": outcome.error,
                "snapshot_id": outcome.snapshot_id,
            }
        )
        if row["status"] != "running":
            if row["outcome_json"] != encoded:
                raise SessionConflict("Run already has a different committed outcome")
            return CompletionReceipt(
                outcome.token.run_id,
                outcome.token.session_id,
                outcome.branch_id,
                outcome.status,
                row["head_id"],
                row["watermark"],
                row["snapshot_id"],
            )
        self.assert_token(connection, outcome.token, allow_revoked=outcome.status == "cancelled")
        if connection.execute(
            "SELECT 1 FROM managed_processes WHERE session_id=? "
            "AND status IN ('launching','running') LIMIT 1",
            (outcome.token.session_id,),
        ).fetchone():
            raise SessionConflict("Session has unresolved process ownership")
        revoked = connection.execute(
            "SELECT 1 FROM execution_revocations WHERE run_id=?", (outcome.token.run_id,)
        ).fetchone()
        if revoked and outcome.entries:
            raise SessionConflict("Revoked runs cannot append final history")
        if outcome.snapshot_id is not None:
            snapshot = connection.execute(
                "SELECT run_id,session_id,branch_id FROM context_snapshots WHERE snapshot_id=?",
                (outcome.snapshot_id,),
            ).fetchone()
            if snapshot is None or tuple(snapshot) != (
                outcome.token.run_id,
                outcome.token.session_id,
                outcome.branch_id,
            ):
                raise SessionConflict("Completion snapshot does not belong to this run")
        receipt = self.append_in_transaction(
            connection,
            outcome.entries,
            token=outcome.token,
            branch_id=outcome.branch_id,
            expected_head=outcome.expected_head,
            allow_revoked=outcome.status == "cancelled",
        )
        watermark = connection.execute(
            "SELECT last_seq FROM sessions WHERE session_id=?", (outcome.token.session_id,)
        ).fetchone()[0]
        connection.execute(
            """UPDATE executions SET status=?, finished_at=?, head_id=?, watermark=?,
               outcome_json=?, error=?, snapshot_id=? WHERE run_id=?""",
            (
                outcome.status,
                self.clock(),
                receipt.head_id,
                watermark,
                encoded,
                outcome.error,
                outcome.snapshot_id,
                outcome.token.run_id,
            ),
        )
        connection.execute(
            "UPDATE sessions SET generation=generation+1, active_run_id=? WHERE session_id=?",
            (f"idle-{outcome.token.run_id}", outcome.token.session_id),
        )
        if self.fault is not None:
            self.fault("outcome_updated")
        return CompletionReceipt(
            outcome.token.run_id,
            outcome.token.session_id,
            outcome.branch_id,
            outcome.status,
            receipt.head_id,
            watermark,
            outcome.snapshot_id,
        )

    async def complete_run(self, outcome: RunOutcome) -> CompletionReceipt:
        frozen = RunOutcome(
            outcome.token,
            outcome.branch_id,
            outcome.status,
            outcome.expected_head,
            tuple(e.model_copy(deep=True) for e in outcome.entries),
            outcome.error,
            outcome.snapshot_id,
        )
        return await self.database.run(
            lambda connection: self.complete_in_transaction(connection, frozen), write=True
        )

    async def list_sessions(
        self, *, principal_id: str, project_id: str | None = None, limit: int = 100
    ) -> list[SessionRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("Session page size must be between 1 and 1000")

        def read(connection: sqlite3.Connection) -> list[SessionRecord]:
            rows = connection.execute(
                """SELECT session_id FROM sessions WHERE principal_id=?
                   AND (? IS NULL OR project_id=?) ORDER BY updated_at DESC, session_id LIMIT ?""",
                (principal_id, project_id, project_id, limit),
            ).fetchall()
            return [self._record(connection, row[0]) for row in rows]

        return await self.database.run(read)

    async def claim(
        self,
        session_id: str,
        *,
        owner_id: str,
        run_id: str,
        ttl_seconds: float = 3600,
        takeover: bool = False,
    ) -> RunToken:
        if not owner_id or not run_id or ttl_seconds <= 0:
            raise ValueError("An owner, run and positive lease duration are required")

        def claim(connection: sqlite3.Connection) -> RunToken:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            if (
                row["recovery_required"]
                or connection.execute(
                    "SELECT 1 FROM managed_processes WHERE session_id=? "
                    "AND status IN ('launching','running') LIMIT 1",
                    (session_id,),
                ).fetchone()
            ):
                raise SessionConflict(
                    "Session requires process/workspace recovery before reopening"
                )
            now = self.clock()
            if row["owner_active"] and row["owner_expires_at"] > now and not takeover:
                raise SessionConflict(f"Session already has an active owner: {session_id}")
            # A vanished writer cannot certify which tool side effects completed.
            # Preserve its attempt and require explicit user work after recovery.
            connection.execute(
                """UPDATE executions SET status='outcome_unknown', finished_at=?,
                   error='Previous writer ended without a completion receipt'
                   WHERE session_id=? AND status='running'""",
                (now, session_id),
            )
            generation = row["generation"] + 1
            connection.execute(
                """UPDATE sessions SET generation=?, owner_id=?, active_run_id=?,
                   owner_expires_at=?, owner_active=1 WHERE session_id=?""",
                (generation, owner_id, run_id, now + ttl_seconds, session_id),
            )
            return RunToken(session_id, owner_id, run_id, generation)

        return await self.database.run(claim, write=True)

    def assert_token(
        self, connection: sqlite3.Connection, token: RunToken, *, allow_revoked: bool = False
    ) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM sessions WHERE session_id=?", (token.session_id,)
        ).fetchone()
        if (
            row is None
            or not row["owner_active"]
            or row["owner_id"] != token.owner_id
            or row["active_run_id"] != token.run_id
            or row["generation"] != token.generation
            or row["owner_expires_at"] <= self.clock()
        ):
            raise StaleRunToken(f"Run no longer owns session {token.session_id}")
        if (
            not allow_revoked
            and connection.execute(
                "SELECT 1 FROM execution_revocations WHERE run_id=?", (token.run_id,)
            ).fetchone()
        ):
            raise StaleRunToken("Run write authority was revoked")
        return row

    async def renew(self, token: RunToken, *, ttl_seconds: float = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Lease duration must be positive")

        def renew(connection: sqlite3.Connection) -> None:
            self.assert_token(connection, token, allow_revoked=True)
            connection.execute(
                "UPDATE sessions SET owner_expires_at=? WHERE session_id=?",
                (self.clock() + ttl_seconds, token.session_id),
            )

        await self.database.run(renew, write=True)

    async def release(self, token: RunToken) -> None:
        def release(connection: sqlite3.Connection) -> None:
            self.assert_token(connection, token, allow_revoked=True)
            connection.execute(
                "UPDATE sessions SET owner_active=0 WHERE session_id=?", (token.session_id,)
            )

        await self.database.run(release, write=True)

    @staticmethod
    def head_in_transaction(
        connection: sqlite3.Connection, session_id: str, branch_id: str
    ) -> BranchHead:
        row = connection.execute(
            "SELECT head_id FROM branches WHERE session_id=? AND branch_id=?",
            (session_id, branch_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown branch: {session_id}/{branch_id}")
        return BranchHead(session_id, branch_id, row[0])

    async def get_head(self, session_id: str, branch_id: str = "main") -> BranchHead:
        return await self.database.run(
            lambda connection: self.head_in_transaction(connection, session_id, branch_id)
        )

    async def append_entries(
        self,
        entries: Sequence[SessionEntry],
        *,
        token: RunToken,
        branch_id: str = "main",
        expected_head: str | None,
    ) -> AppendReceipt:
        # Freeze input before yielding; the caller cannot change an admitted write.
        frozen = tuple(entry.model_copy(deep=True) for entry in entries)
        return await self.database.run(
            lambda connection: self.append_in_transaction(
                connection, frozen, token=token, branch_id=branch_id, expected_head=expected_head
            ),
            write=True,
        )

    def append_in_transaction(
        self,
        connection: sqlite3.Connection,
        entries: Sequence[SessionEntry],
        *,
        token: RunToken,
        branch_id: str = "main",
        expected_head: str | None,
        allow_revoked: bool = False,
    ) -> AppendReceipt:
        """Compose with a host's final outcome and Outbox in this same transaction."""
        row = self.assert_token(connection, token, allow_revoked=allow_revoked)
        head = self.head_in_transaction(connection, token.session_id, branch_id)
        if len({entry.id for entry in entries}) != len(entries):
            raise SessionConflict("A batch contains duplicate entry IDs")
        parent = expected_head
        encoded: list[str] = []
        existing: list[sqlite3.Row | None] = []
        for entry in entries:
            if entry.parent_id != parent:
                raise SessionConflict("Batch does not extend the expected parent chain")
            parent = entry.id
            body = entry_body(entry)
            encoded.append(body)
            stored = connection.execute(
                "SELECT * FROM entries WHERE session_id=? AND entry_id=?",
                (token.session_id, entry.id),
            ).fetchone()
            if stored is not None and (
                stored["body_json"] != body or stored["origin_branch_id"] != branch_id
            ):
                raise SessionConflict(f"Entry ID has different content or branch: {entry.id}")
            existing.append(stored)
        if entries and all(item is not None for item in existing):
            return AppendReceipt(
                token.session_id,
                branch_id,
                entries[-1].id,
                tuple(entry.id for entry in entries),
                tuple(item["seq"] for item in existing if item is not None),
                False,
            )
        if any(item is not None for item in existing):
            raise SessionConflict("A retry must match the original atomic batch")
        if head.entry_id != expected_head:
            raise SessionConflict(
                f"Branch head changed: expected {expected_head}, got {head.entry_id}"
            )
        sequence = row["last_seq"]
        sequences: list[int] = []
        for entry, body in zip(entries, encoded, strict=True):
            sequence += 1
            if entry.seq is not None and entry.seq != sequence:
                raise SessionConflict("Supplied sequence does not match the allocated sequence")
            sequences.append(sequence)
            connection.execute(
                """INSERT INTO entries(session_id, entry_id, seq, parent_id, origin_branch_id,
                   run_id, kind, body_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token.session_id,
                    entry.id,
                    sequence,
                    entry.parent_id,
                    branch_id,
                    token.run_id,
                    entry.type,
                    body,
                ),
            )
            if self.fault is not None:
                self.fault("entry_inserted")
        if entries:
            connection.execute(
                "UPDATE branches SET head_id=? WHERE session_id=? AND branch_id=?",
                (entries[-1].id, token.session_id, branch_id),
            )
            connection.execute(
                "UPDATE sessions SET last_seq=?, updated_at=? WHERE session_id=?",
                (sequence, self.clock(), token.session_id),
            )
            if self.fault is not None:
                self.fault("head_updated")
        return AppendReceipt(
            token.session_id,
            branch_id,
            parent,
            tuple(entry.id for entry in entries),
            tuple(sequences),
            bool(entries),
        )

    async def read_entries(
        self,
        session_id: str,
        *,
        branch_id: str | None = None,
        after_seq: int = 0,
        through_seq: int | None = None,
        limit: int = 1000,
    ) -> EntryPage:
        if (
            not 1 <= limit <= 10_000
            or after_seq < 0
            or (through_seq is not None and through_seq < 0)
        ):
            raise ValueError("Invalid history page bounds")

        def read(connection: sqlite3.Connection) -> EntryPage:
            parameters: list[Any]
            if branch_id is None:
                self._record(connection, session_id)
                query = "SELECT * FROM entries WHERE session_id=?"
                parameters = [session_id]
            else:
                head = self.head_in_transaction(connection, session_id, branch_id)
                query = """WITH RECURSIVE ancestors AS (
                    SELECT * FROM entries WHERE session_id=? AND entry_id=?
                    UNION ALL SELECT entry.* FROM entries entry JOIN ancestors child
                    ON entry.session_id=child.session_id AND entry.entry_id=child.parent_id
                ) SELECT * FROM ancestors WHERE 1=1"""
                parameters = [session_id, head.entry_id]
            query += " AND seq>? AND (? IS NULL OR seq<=?) ORDER BY seq LIMIT ?"
            parameters.extend([after_seq, through_seq, through_seq, limit + 1])
            rows = connection.execute(query, parameters).fetchall()
            page = rows[:limit]
            return EntryPage(
                tuple(decode_entry(item) for item in page),
                page[-1]["seq"] if len(rows) > limit else None,
            )

        return await self.database.run(read)

    async def fork_branch(
        self,
        *,
        token: RunToken,
        branch_id: str,
        at_entry_id: str | None,
        source_branch_id: str = "main",
    ) -> BranchHead:
        if not branch_id:
            raise ValueError("Branch identity must not be empty")

        def fork(connection: sqlite3.Connection) -> BranchHead:
            self.assert_token(connection, token)
            head = self.head_in_transaction(connection, token.session_id, source_branch_id)
            if at_entry_id is not None and not self._ancestor(connection, head, at_entry_id):
                raise SessionConflict("Fork entry is not an ancestor of the source branch")
            try:
                connection.execute(
                    """INSERT INTO branches(session_id, branch_id, parent_branch_id,
                       fork_entry_id, head_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        token.session_id,
                        branch_id,
                        source_branch_id,
                        at_entry_id,
                        at_entry_id,
                        self.clock(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionConflict(f"Branch already exists: {branch_id}") from exc
            return BranchHead(token.session_id, branch_id, at_entry_id)

        return await self.database.run(fork, write=True)

    @staticmethod
    def _ancestor(connection: sqlite3.Connection, head: BranchHead, entry_id: str) -> bool:
        row = connection.execute(
            """WITH RECURSIVE ancestors(entry_id, parent_id) AS (
               SELECT entry_id, parent_id FROM entries WHERE session_id=? AND entry_id=?
               UNION ALL SELECT e.entry_id, e.parent_id FROM entries e JOIN ancestors a
               ON e.session_id=? AND e.entry_id=a.parent_id)
               SELECT 1 FROM ancestors WHERE entry_id=? LIMIT 1""",
            (head.session_id, head.entry_id, head.session_id, entry_id),
        ).fetchone()
        return row is not None

    async def put_snapshot(
        self,
        *,
        token: RunToken,
        branch_id: str,
        expected_head: str | None,
        builder_version: str,
        payload: dict[str, Any],
    ) -> str:
        frozen, blocks = encode_context(payload)
        digest = hashlib.sha256(frozen.encode()).hexdigest()
        snapshot_id = uuid4().hex

        def put(connection: sqlite3.Connection) -> str:
            self.assert_token(connection, token)
            head = self.head_in_transaction(connection, token.session_id, branch_id)
            if head.entry_id != expected_head:
                raise SessionConflict("Snapshot history changed before commit")
            store_blocks(connection, blocks)
            watermark = (
                0
                if head.entry_id is None
                else connection.execute(
                    "SELECT seq FROM entries WHERE session_id=? AND entry_id=?",
                    (token.session_id, head.entry_id),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO context_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    token.run_id,
                    token.session_id,
                    branch_id,
                    expected_head,
                    watermark,
                    builder_version,
                    digest,
                    frozen,
                    self.clock(),
                ),
            )
            return snapshot_id

        return await self.database.run(put, write=True)

    async def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        return await self.database.run(lambda connection: read_snapshot(connection, snapshot_id))
