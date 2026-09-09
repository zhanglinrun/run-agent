"""Bounded observations and durable accounting on the host's SQLite worker."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from run_agent_coding.storage.settle import settle
from run_agent_coding.storage.sqlite import SqliteDatabase


class TelemetryUnavailable(RuntimeError):
    pass


class SqliteTelemetrySink:
    """Optional spans may drop; accounting waits for a committed short transaction.

    Neither path stores request bodies or selects another persistence backend.
    Callers must flush/close before archiving; an unclean exit can lose queued spans.
    """

    def __init__(
        self,
        database: SqliteDatabase,
        *,
        max_pending: int = 1024,
        max_record_bytes: int = 65536,
        batch_size: int = 64,
    ) -> None:
        if min(max_pending, max_record_bytes, batch_size) < 1:
            raise ValueError("Observation queue bounds must be positive")
        self.database = database
        self.sink_id = uuid4().hex
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(max_pending)
        self._limit = max_record_bytes
        self._batch_size = batch_size
        self._closed = False
        self._error: BaseException | None = None
        self.dropped = 0
        self.failed = 0
        self._loop = asyncio.get_running_loop()
        self._writes: set[asyncio.Task[None]] = set()
        self._writer = asyncio.create_task(self._drain(), name="sqlite-observations")
        self._close_task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        return self.database.path

    def _freeze(self, stream: str, payload: Mapping[str, Any]) -> str:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Observation sinks belong to their host event loop")
        if not stream or len(stream.encode("utf-8")) > 256:
            raise ValueError("Observation stream identity must be nonempty and at most 256 bytes")
        return json.dumps(dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def emit(self, stream: str, payload: Mapping[str, Any]) -> bool:
        body = self._freeze(stream, payload)
        if (
            self._closed
            or self._error is not None
            or self._queue.full()
            or len(body.encode("utf-8")) > self._limit
        ):
            self.dropped += 1
            return False
        self._queue.put_nowait((stream, body))
        return True

    async def append(self, stream: str, payload: Mapping[str, Any]) -> None:
        body = self._freeze(stream, payload)
        if self._closed or self._error is not None or len(body.encode("utf-8")) > self._limit:
            self.failed += 1
            raise TelemetryUnavailable(
                "Required observation could not be admitted"
            ) from self._error
        operation = asyncio.create_task(
            self.database.run(partial(_write_batch, batch=((stream, body),)), write=True)
        )
        self._writes.add(operation)
        try:

            async def wait_write() -> None:
                await operation

            _, cancelled = await settle(wait_write())
        except BaseException as exc:
            self.failed += 1
            self._error = exc
            raise TelemetryUnavailable("Accounting persistence failed") from exc
        finally:
            self._writes.discard(operation)
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            batch = [item]
            # Closing sends the sentinel only after every queued span has settled.
            while len(batch) < self._batch_size and not self._queue.empty():
                additional = self._queue.get_nowait()
                assert additional is not None
                batch.append(additional)
            try:
                await self.database.run(partial(_write_batch, batch=tuple(batch)), write=True)
            except BaseException as exc:
                self._error = exc
                self.failed += len(batch)
                # A failed writer still drains accepted work, so close cannot hang.
                # Further producers fail admission and flush exposes this error.
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()
        if self._writes:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._writes)))
        if self._error is not None:
            raise TelemetryUnavailable("Observation persistence failed") from self._error
        sink_id, dropped, failed = self.sink_id, self.dropped, self.failed

        def health(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO observation_health(sink_id,dropped,failed,updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(sink_id) DO UPDATE SET dropped=max(dropped,excluded.dropped), "
                "failed=max(failed,excluded.failed),updated_at=excluded.updated_at",
                (sink_id, dropped, failed, time()),
            )

        await self.database.run(health, write=True)

    async def read(
        self,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if after_seq < 0 or not 1 <= limit <= 10000:
            raise ValueError("Invalid observation page bounds")

        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT seq,body_json FROM observations "
                "WHERE stream=? AND seq>? ORDER BY seq LIMIT ?",
                (stream, after_seq, limit),
            ).fetchall()
            return [{**json.loads(row["body_json"]), "seq": row["seq"]} for row in rows]

        return await self.database.run(read)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())

        async def wait_close() -> None:
            assert self._close_task is not None
            await self._close_task

        _, cancelled = await settle(wait_close())
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        try:
            await self.flush()
        finally:
            await self._queue.put(None)
            await self._writer


def _write_batch(connection: sqlite3.Connection, *, batch: tuple[tuple[str, str], ...]) -> None:
    connection.executemany(
        "INSERT INTO observations(stream,body_json,created_at) VALUES (?,?,?)",
        [(stream, body, time()) for stream, body in batch],
    )
