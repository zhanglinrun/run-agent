"""Locked, append-only session storage implementations."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Protocol

from run_agent_core.session.entries import SessionEntry
from run_agent_core.session.jsonl import (
    SessionJsonlError,
    entry_from_json_line,
    entry_to_json_line,
)


class SessionStorage(Protocol):
    """Append-only session storage interface."""

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry to storage."""
        ...

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append a complete batch of entries."""
        ...

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in storage order."""
        ...


class _TornTailError(Exception):
    def __init__(self, *, truncate_at: int) -> None:
        self.truncate_at = truncate_at


class JsonlSessionStorage:
    """Strict JSONL storage with locking, sequencing, and torn-tail repair.

    New entries receive a one-based continuous ``seq`` under the same
    cross-process lock as the write. A malformed final record is recoverable
    only when it is an unterminated tail; corruption elsewhere remains a hard
    error. Batch writes use a same-directory temporary file, fsync, and atomic
    replace so related session entries become visible together.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")

    async def append(self, entry: SessionEntry) -> None:
        self.append_sync(entry)

    def append_sync(self, entry: SessionEntry) -> None:
        """Synchronous append used by command paths that cannot await."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            existing = self._read_repair_unlocked()
            self._assign_sequences((entry,), start=len(existing) + 1)
            with self.path.open("ab") as file:
                file.write(entry_to_json_line(entry).encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        self.append_batch_sync(entries)

    def append_batch_sync(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append all entries, preserving the old file on failure."""
        batch = tuple(entries)
        if not batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            existing = self._read_repair_unlocked()
            self._assign_sequences(batch, start=len(existing) + 1)
            encoded = b"".join(entry_to_json_line(entry).encode("utf-8") for entry in batch)
            previous = self.path.read_bytes() if self.path.exists() else b""
            self._atomic_replace(previous + encoded)

    async def read_all(self) -> list[SessionEntry]:
        """Read and validate all entries, repairing only a torn final record."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_path.exists():
            with self._locked(exclusive=True):
                self._remove_incomplete_temp()
                return self._read_repair_unlocked()
        try:
            with self._locked(exclusive=False):
                return self._read_unlocked()
        except _TornTailError:
            with self._locked(exclusive=True):
                return self._read_repair_unlocked()

    @staticmethod
    def _assign_sequences(entries: Sequence[SessionEntry], *, start: int) -> None:
        for offset, entry in enumerate(entries):
            expected = start + offset
            if entry.seq is not None and entry.seq != expected:
                raise SessionJsonlError(
                    f"Invalid session sequence for entry {entry.id}: "
                    f"expected {expected}, got {entry.seq}"
                )
        for offset, entry in enumerate(entries):
            entry.seq = start + offset

    def _read_repair_unlocked(self) -> list[SessionEntry]:
        try:
            return self._read_unlocked()
        except _TornTailError as exc:
            if not self.path.exists():
                return []
            with self.path.open("r+b") as file:
                file.truncate(exc.truncate_at)
                file.flush()
                os.fsync(file.fileno())
            return self._read_unlocked()

    def _read_unlocked(self) -> list[SessionEntry]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        raw_lines = data.split(b"\n")
        entries: list[SessionEntry] = []
        seen_ids: set[str] = set()
        last_index = len(raw_lines) - 1
        truncate_at = data.rfind(b"\n") + 1
        for index, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
                entry = entry_from_json_line(line, line_number=index)
            except (UnicodeDecodeError, SessionJsonlError) as exc:
                if index - 1 == last_index and not data.endswith(b"\n"):
                    raise _TornTailError(truncate_at=truncate_at) from exc
                if isinstance(exc, SessionJsonlError):
                    raise
                raise SessionJsonlError(
                    f"Invalid UTF-8 session entry on line {index}: {exc}"
                ) from exc
            expected_seq = len(entries) + 1
            if entry.seq is not None and entry.seq != expected_seq:
                raise SessionJsonlError(
                    f"Invalid session sequence on line {index}: "
                    f"expected {expected_seq}, got {entry.seq}"
                )
            if entry.id in seen_ids:
                raise SessionJsonlError(f"Duplicate session entry id {entry.id!r} on line {index}")
            seen_ids.add(entry.id)
            entries.append(entry)
        return entries

    def _atomic_replace(self, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            os.close(descriptor)
            temporary_path = Path(temporary)
            with temporary_path.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            with _suppress_os_error():
                Path(temporary).unlink()
            raise

    def _remove_incomplete_temp(self) -> None:
        with _suppress_os_error():
            self.temp_path.unlink()
        prefix = f".{self.path.name}."
        for candidate in self.path.parent.glob(f"{prefix}*.tmp"):
            with _suppress_os_error():
                candidate.unlink()

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            with suppress(OSError):
                os.chmod(self.lock_path, 0o600)
            _lock_file(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(lock_file)


class InMemorySessionStorage:
    """Deterministic storage useful for tests and embedded frontends."""

    def __init__(self, entries: Sequence[SessionEntry] = ()) -> None:
        self.entries = list(entries)
        self._lock = asyncio.Lock()

    async def append(self, entry: SessionEntry) -> None:
        async with self._lock:
            self.entries.append(entry)

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        async with self._lock:
            self.entries.extend(entries)

    async def read_all(self) -> list[SessionEntry]:
        async with self._lock:
            return list(self.entries)


@contextmanager
def _suppress_os_error() -> Iterator[None]:
    with suppress(OSError):
        yield


def _lock_file(file: BinaryIO, *, exclusive: bool) -> None:
    if os.name == "nt":
        msvcrt = import_module("msvcrt")

        del exclusive
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl = import_module("fcntl")

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(file.fileno(), mode)


def _unlock_file(file: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = import_module("msvcrt")

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = import_module("fcntl")

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InMemorySessionStorage", "JsonlSessionStorage", "SessionStorage"]
