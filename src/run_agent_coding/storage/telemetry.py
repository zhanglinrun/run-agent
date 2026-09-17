"""Bounded observations and durable accounting as append-only JSONL."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from run_agent_coding.storage.settle import settle


class TelemetryUnavailable(RuntimeError):
    pass


def _write_batch(path: Path, *, batch: tuple[tuple[str, str], ...], start_seq: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        seq = start_seq
        for stream, body in batch:
            seq += 1
            payload = json.loads(body)
            file.write(
                json.dumps(
                    {"stream": stream, "seq": seq, "created_at": time(), "body": payload},
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        file.flush()
        import os

        os.fsync(file.fileno())
    return start_seq + len(batch)


def _max_seq(path: Path) -> int:
    if not path.exists():
        return 0
    last = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("seq"), int):
            last = max(last, payload["seq"])
    return last


class JsonlTelemetrySink:
    """Optional spans may drop; accounting waits for a durable append."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_pending: int = 1024,
        max_record_bytes: int = 65536,
        batch_size: int = 64,
    ) -> None:
        if min(max_pending, max_record_bytes, batch_size) < 1:
            raise ValueError("Observation queue bounds must be positive")
        self._path = Path(path)
        self.sink_id = uuid4().hex
        self._queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(max_pending)
        self._limit = max_record_bytes
        self._batch_size = batch_size
        self._closed = False
        self._error: BaseException | None = None
        self.dropped = 0
        self.failed = 0
        self._seq = _max_seq(self._path)
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._writes: set[asyncio.Task[None]] = set()
        self._writer = asyncio.create_task(self._drain(), name="jsonl-observations")
        self._close_task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        return self._path

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

        async def write() -> None:
            async with self._lock:
                self._seq = _write_batch(self._path, batch=((stream, body),), start_seq=self._seq)

        operation = asyncio.create_task(write())
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
            while len(batch) < self._batch_size and not self._queue.empty():
                additional = self._queue.get_nowait()
                assert additional is not None
                batch.append(additional)
            try:
                async with self._lock:
                    self._seq = _write_batch(self._path, batch=tuple(batch), start_seq=self._seq)
            except BaseException as exc:
                self._error = exc
                self.failed += len(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def flush(self) -> None:
        await self._queue.join()
        if self._writes:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._writes)))
        if self._error is not None:
            raise TelemetryUnavailable("Observation persistence failed") from self._error

    async def read(
        self,
        stream: str,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if after_seq < 0 or not 1 <= limit <= 10000:
            raise ValueError("Invalid observation page bounds")
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("stream") != stream:
                continue
            seq = int(payload.get("seq") or 0)
            if seq <= after_seq:
                continue
            body = payload.get("body")
            record = dict(body) if isinstance(body, dict) else {"body": body}
            record["seq"] = seq
            rows.append(record)
            if len(rows) >= limit:
                break
        return rows

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
