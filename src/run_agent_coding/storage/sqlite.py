"""One SQLite worker with bounded admission and explicit transaction boundaries."""

from __future__ import annotations

import asyncio
import inspect
import queue
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

SCHEMA_VERSION = 7
APPLICATION_ID = 1381322305
T = TypeVar("T")


class DatabaseClosed(RuntimeError):
    """New work cannot enter a closing or closed database."""


class SchemaMismatch(RuntimeError):
    """This application cannot interpret the database's current schema."""


@dataclass(slots=True)
class _Request:
    operation: Callable[[sqlite3.Connection], Any]
    write: bool
    result: Future[Any]


class SqliteDatabase:
    """Own a connection on one worker; never run SQL on the asyncio thread.

    Cancellation after admission stops waiting, not the transaction. The admitted
    operation remains bounded and completes or rolls back. Callers use stable
    event IDs to resolve an uncertain receipt. No transaction may await a tool.
    """

    def __init__(self, path: Path, *, max_pending: int, busy_timeout_ms: int) -> None:
        if max_pending < 1 or busy_timeout_ms < 0:
            raise ValueError("Invalid database queue size or busy timeout")
        self.path = path.resolve()
        self._permits = asyncio.BoundedSemaphore(max_pending)
        # Reserve one slot for the closing sentinel, even under full admission.
        self._requests: queue.Queue[_Request | None] = queue.Queue(max_pending + 1)
        self._ready: Future[None] = Future()
        self._stopped: Future[None] = Future()
        self._closing = False
        self._loop = asyncio.get_running_loop()
        self._busy_timeout_ms = busy_timeout_ms
        self._thread = threading.Thread(target=self._work, name="run-sqlite", daemon=True)
        self._thread.start()

    @classmethod
    async def open(
        cls, path: str | Path, *, max_pending: int = 128, busy_timeout_ms: int = 5000
    ) -> SqliteDatabase:
        database = cls(Path(path), max_pending=max_pending, busy_timeout_ms=busy_timeout_ms)
        try:
            await asyncio.shield(asyncio.wrap_future(database._ready))
        except BaseException:
            await database.aclose()
            raise
        return database

    async def run(self, operation: Callable[[sqlite3.Connection], T], *, write: bool = False) -> T:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("A database belongs to one host event loop")
        if self._closing:
            raise DatabaseClosed("Database is closing")
        await self._permits.acquire()
        if self._closing:
            self._permits.release()
            raise DatabaseClosed("Database is closing")
        result: Future[T] = Future()
        future = asyncio.wrap_future(result)

        def completed(done: asyncio.Future[T]) -> None:
            self._permits.release()
            # Retrieve even an abandoned failure; a caller cancelled after admission.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(completed)
        self._requests.put_nowait(_Request(operation, write, result))
        return await asyncio.shield(future)

    async def aclose(self) -> None:
        if not self._closing:
            self._closing = True
            if not self._stopped.done():
                self._requests.put_nowait(None)
        await asyncio.shield(asyncio.wrap_future(self._stopped))

    async def __aenter__(self) -> SqliteDatabase:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version == 0 and not tables and app_id == 0:
                schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or app_id != APPLICATION_ID:
                raise SchemaMismatch(
                    f"Unsupported database schema: app={app_id}, version={version}"
                )
            connection.commit()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _work(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            self._ready.set_result(None)
            while (request := self._requests.get()) is not None:
                try:
                    connection.execute("BEGIN IMMEDIATE" if request.write else "BEGIN")
                    value = request.operation(connection)
                    if inspect.isawaitable(value):
                        if inspect.iscoroutine(value):
                            value.close()
                        raise TypeError(
                            "Database operations must be synchronous short transactions"
                        )
                    connection.commit()
                except BaseException as exc:
                    connection.rollback()
                    request.result.set_exception(exc)
                else:
                    request.result.set_result(value)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            # Unblock every admitted request if the worker itself cannot continue.
            while not self._requests.empty():
                pending = self._requests.get_nowait()
                if pending is not None:
                    pending.result.set_exception(exc)
        finally:
            if connection is not None:
                connection.close()
            self._stopped.set_result(None)
