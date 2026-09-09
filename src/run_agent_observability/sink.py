"""Host-injected observation storage; no filesystem backend is selected here."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol


class TelemetrySink(Protocol):
    @property
    def path(self) -> Path: ...

    def emit(self, stream: str, payload: Mapping[str, Any]) -> bool: ...

    async def append(self, stream: str, payload: Mapping[str, Any]) -> None:
        """Wait for durable admission and commit of an accounting record."""
        ...

    async def flush(self) -> None: ...

    async def read(
        self, stream: str, *, after_seq: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]: ...


class ScopedTelemetrySink:
    def __init__(self, sink: TelemetrySink, prefix: str, assert_active: Callable[[], None]) -> None:
        self._sink, self._prefix, self._assert_active = sink, prefix, assert_active

    @property
    def path(self) -> Path:
        self._assert_active()
        return self._sink.path

    def emit(self, stream: str, payload: Mapping[str, Any]) -> bool:
        self._assert_active()
        return self._sink.emit(self._prefix + stream, payload)

    async def append(self, stream: str, payload: Mapping[str, Any]) -> None:
        self._assert_active()
        await self._sink.append(self._prefix + stream, payload)

    async def flush(self) -> None:
        self._assert_active()
        await self._sink.flush()

    async def read(
        self, stream: str, *, after_seq: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        self._assert_active()
        result = await self._sink.read(self._prefix + stream, after_seq=after_seq, limit=limit)
        self._assert_active()
        return result


async def read_stream(
    sink: TelemetrySink,
    stream: str,
    *,
    flush: bool = True,
) -> list[dict[str, Any]]:
    """Explicitly materialize a stream for reports and independent audit."""
    if flush:
        await sink.flush()
    records: list[dict[str, Any]] = []
    after = 0
    while True:
        page = await sink.read(stream, after_seq=after)
        records.extend(page)
        if len(page) < 1000:
            return records
        after = page[-1]["seq"]
