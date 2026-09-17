"""Locked, append-only session storage implementations."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from run_agent_core.session.entries import SessionEntry
from run_agent_core.session.jsonl import entries_from_json_lines, entry_to_json_line


class SessionStorage(Protocol):
    """Append-only session storage.

    ``append`` writes one complete line. ``append_batch`` is the durable
    multi-entry boundary: the complete batch is visible, or the previous
    transcript is untouched.
    """

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry to storage."""
        ...

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append a complete batch of entries."""
        ...

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in storage order."""
        ...


async def load_session_entries(storage: SessionStorage) -> list[SessionEntry]:
    """Materialize history for cold open and tree inspection."""
    return await storage.read_all()


def _encoded_entries(entries: Sequence[SessionEntry]) -> bytes:
    return b"".join(entry_to_json_line(entry).encode("utf-8") for entry in entries)


class InMemorySessionStorage:
    """Deterministic storage for tests and embedded frontends."""

    def __init__(
        self, entries: Sequence[SessionEntry] = (), *, session_id: str | None = None
    ) -> None:
        self.session_id = session_id or uuid4().hex
        self.entries = list(entries)
        self._lock = asyncio.Lock()

    async def append(self, entry: SessionEntry) -> None:
        async with self._lock:
            self.entries.append(entry)

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        batch = tuple(entries)
        if not batch:
            return
        async with self._lock:
            self.entries.extend(batch)

    async def read_all(self) -> list[SessionEntry]:
        async with self._lock:
            return list(self.entries)


class JsonlSessionStorage:
    """Local JSONL storage with a per-session cross-process lock.

    The lock is deliberately separate from the transcript. Readers use a
    shared lock when the platform provides one; every write uses an exclusive
    lock. Single appends write the file tail and fsync. Batch writes use a
    same-directory temporary file, fsync, replace, and directory fsync.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry under the session's exclusive cross-process lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = entry_to_json_line(entry).encode("utf-8")
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            with self.path.open("ab") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append all entries, preserving the old file on failure."""
        batch = tuple(entries)
        if not batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = _encoded_entries(batch)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            previous = self.path.read_bytes() if self.path.exists() else b""
            self._atomic_replace(previous + encoded)

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in file order; missing files are empty sessions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_path.exists():
            with self._locked(exclusive=True):
                self._remove_incomplete_temp()
                return self._read_unlocked()
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> list[SessionEntry]:
        if not self.path.exists():
            return []
        return entries_from_json_lines(self.path.read_text(encoding="utf-8").split("\n"))

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
            os.chmod(self.lock_path, 0o600)
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _lock_file(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(lock_file)


@contextmanager
def _suppress_os_error() -> Iterator[None]:
    with suppress(OSError):
        yield


def _lock_file(file: BinaryIO, *, exclusive: bool) -> None:
    if os.name == "nt":
        import msvcrt

        del exclusive
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(file.fileno(), mode)


def _unlock_file(file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

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
