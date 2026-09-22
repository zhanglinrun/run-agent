"""Locked, append-only session storage implementations."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from run_agent_core.session.contracts import SessionConflict
from run_agent_core.session.entries import SessionEntry
from run_agent_core.session.jsonl import entries_from_json_lines, entry_to_json_line
from run_agent_core.session.tree import resolve_active_leaf_id


@dataclass(slots=True)
class StorageDiagnostics:
    """Counters for storage failures that are deliberately tolerated.

    A directory ``fsync`` is advisory: Windows cannot open a directory for it and some
    filesystems refuse the call. The data file itself is already durable, so the failure
    must not fail the write - but it must not vanish either, because "the rename is
    durable" is exactly the claim an operator needs to audit. Every caller that uses the
    default sink can read these counters; tests inject their own instance.
    """

    directory_fsync_attempts: int = 0
    directory_fsync_failures: int = 0
    last_directory_fsync_operation: str | None = None
    last_directory_fsync_path: str | None = None
    last_directory_fsync_error: str | None = None

    def record_directory_fsync(self, path: Path, *, operation: str, error: OSError | None) -> None:
        """Record one attempt; ``error`` is ``None`` when the directory flush succeeded."""
        self.directory_fsync_attempts += 1
        if error is None:
            return
        self.directory_fsync_failures += 1
        self.last_directory_fsync_operation = operation
        self.last_directory_fsync_path = str(path)
        self.last_directory_fsync_error = f"{type(error).__name__}: {error}"


DEFAULT_STORAGE_DIAGNOSTICS = StorageDiagnostics()


def storage_diagnostics() -> StorageDiagnostics:
    """Return the process-wide sink used by storages that were not given their own."""
    return DEFAULT_STORAGE_DIAGNOSTICS


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

    async def compare_and_append(
        self, entries: Sequence[SessionEntry], expected_head: str | None
    ) -> list[SessionEntry]:
        """Validate and append under one lock, returning the numbered committed snapshot."""
        ...

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in storage order."""
        ...


async def load_session_entries(storage: SessionStorage) -> list[SessionEntry]:
    """Materialize history for cold open and tree inspection."""
    return await storage.read_all()


def _encoded_entries(entries: Sequence[SessionEntry]) -> bytes:
    return b"".join(entry_to_json_line(entry).encode("utf-8") for entry in entries)


def _prepare_compare_and_append(
    existing: Sequence[SessionEntry],
    entries: Sequence[SessionEntry],
    expected_head: str | None,
) -> tuple[list[SessionEntry], tuple[SessionEntry, ...]]:
    numbered = [
        entry.model_copy(deep=True, update={"seq": index})
        for index, entry in enumerate(existing, start=1)
    ]
    if resolve_active_leaf_id(numbered) != expected_head:
        raise SessionConflict("expected_head mismatch")

    known: set[str] = set()
    for entry in numbered:
        if entry.id in known:
            raise SessionConflict(f"Duplicate session entry id: {entry.id}")
        known.add(entry.id)

    appended: list[SessionEntry] = []
    for entry in entries:
        if entry.id in known:
            raise SessionConflict(f"Duplicate session entry id: {entry.id}")
        if entry.parent_id is not None and entry.parent_id not in known:
            raise SessionConflict(f"Invalid parent_id: {entry.parent_id}")
        committed = entry.model_copy(deep=True, update={"seq": len(numbered) + 1})
        numbered.append(committed)
        appended.append(committed)
        known.add(entry.id)
    return numbered, tuple(appended)


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

    async def compare_and_append(
        self, entries: Sequence[SessionEntry], expected_head: str | None
    ) -> list[SessionEntry]:
        batch = tuple(entries)
        async with self._lock:
            numbered, appended = _prepare_compare_and_append(self.entries, batch, expected_head)
            self.entries.extend(appended)
            return numbered

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

    def __init__(self, path: str | Path, *, diagnostics: StorageDiagnostics | None = None) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")
        self.diagnostics = diagnostics if diagnostics is not None else DEFAULT_STORAGE_DIAGNOSTICS

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

    async def compare_and_append(
        self, entries: Sequence[SessionEntry], expected_head: str | None
    ) -> list[SessionEntry]:
        """Compare the active head and atomically append while holding the file lock."""
        batch = tuple(entries)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            existing = self._read_unlocked()
            numbered, appended = _prepare_compare_and_append(existing, batch, expected_head)
            if appended:
                previous = self.path.read_bytes() if self.path.exists() else b""
                self._atomic_replace(previous + _encoded_entries(appended))
            return numbered

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
            _fsync_directory(self.path.parent, diagnostics=self.diagnostics)
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
    if sys.platform == "win32":
        import msvcrt

        del exclusive
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(file.fileno(), mode)


def _unlock_file(file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path, *, diagnostics: StorageDiagnostics | None = None) -> None:
    """Flush a renamed directory entry, tolerating and recording platform refusals.

    Windows cannot open a directory for ``os.fsync`` and some filesystems refuse the
    call, so a failure here must not fail the write: the data file and its rename are
    already durable. The failure is recorded on ``diagnostics`` instead of being
    silently swallowed, and a success only bumps the attempt counter.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:
        if diagnostics is not None:
            diagnostics.record_directory_fsync(path, operation="open", error=error)
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        if diagnostics is not None:
            diagnostics.record_directory_fsync(path, operation="fsync", error=error)
    else:
        if diagnostics is not None:
            diagnostics.record_directory_fsync(path, operation="fsync", error=None)
    finally:
        os.close(descriptor)
