"""Bounded command output and verified process ownership through cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, BinaryIO, Protocol, cast
from uuid import uuid4

from run_agent_coding.host.process_identity import (
    current_process_identity,
    machine_identity,
    process_identity,
)
from run_agent_coding.storage.settle import settle
from run_agent_core.tools import ToolCancellationToken
from run_agent_core.types import JSONValue

ProcessRecorder = Callable[[dict[str, JSONValue]], Coroutine[Any, Any, None]]


class ProcessCleanupError(RuntimeError):
    """A process group is not verified empty; its owner must retain the workspace."""


class OwnedProcess(Protocol):
    pid: int
    identity: str
    kind: str
    def resume(self) -> None: ...
    def poll(self) -> int | None: ...
    def active_count(self) -> int: ...
    def terminate(self, *, force: bool) -> None: ...
    def close(self) -> None: ...


class PosixProcess:
    kind = "posix_group"

    def __init__(
        self, command: str, cwd: Path, output: BinaryIO, *, bash: bool, identity: str,
    ) -> None:
        self._process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("process_gate.py")),
             identity, "bash" if bash else "/bin/sh", command],
            cwd=cwd, stdin=subprocess.PIPE, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.pid = self._process.pid
        self.identity = str(self.pid)

    def poll(self) -> int | None:
        return self._process.poll()

    def resume(self) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(b"G")
        self._process.stdin.close()

    def active_count(self) -> int:
        self.poll()
        try:
            getattr(os, "killpg")(self.pid, 0)  # noqa: B009 - Windows type stubs
        except ProcessLookupError:
            return 0
        return 1  # Process groups expose presence, not a portable member count.

    def terminate(self, *, force: bool) -> None:
        with suppress(ProcessLookupError):
            getattr(os, "killpg")(  # noqa: B009 - Windows type stubs
                self.pid, getattr(signal, "SIGKILL") if force else signal.SIGTERM  # noqa: B009
            )

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait(timeout=0)


@dataclass(slots=True)
class ProcessExecution:
    process: OwnedProcess
    output: BinaryIO
    events: list[str] = field(default_factory=lambda: ["started"])
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    output: bytes
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output_limited: bool
    pid: int
    identity: str
    kind: str
    events: tuple[str, ...]


class ProcessSupervisor:
    def __init__(self, *, grace_period: float = 0.25, cleanup_timeout: float = 5) -> None:
        if grace_period < 0 or cleanup_timeout <= 0:
            raise ValueError("Process cleanup deadlines must be positive")
        self.grace_period, self.cleanup_timeout = grace_period, cleanup_timeout
        self._active: dict[str, ProcessExecution] = {}
        self._cleanup_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._closed = False
        self.recorder_factory: Callable[[], ProcessRecorder] | None = None

    @property
    def active_count(self) -> int:
        return len(self._active)

    def assert_empty(self) -> None:
        if self._active:
            raise ProcessCleanupError("Owned commands have not verified their process exit")

    async def run(
        self, command: str, *, cwd: Path, timeout: float | None = None,
        cancellation: ToolCancellationToken | None = None, bash: bool = False,
        output_limit: int = 32 * 1024 * 1024,
    ) -> ProcessResult:
        async with self._run_lock:
            return await self._run(
                command, cwd=cwd, timeout=timeout, cancellation=cancellation,
                bash=bash, output_limit=output_limit,
            )

    async def _run(
        self, command: str, *, cwd: Path, timeout: float | None,
        cancellation: ToolCancellationToken | None, bash: bool, output_limit: int,
    ) -> ProcessResult:
        if self._closed:
            raise RuntimeError("Process supervisor is closed")
        self.assert_empty()
        if output_limit < 1 or (timeout is not None and timeout <= 0):
            raise ValueError("Process output/timeout limits must be positive")
        if cancellation is not None and cancellation.is_cancelled():
            raise asyncio.CancelledError
        identity = uuid4().hex
        recorder = self.recorder_factory() if self.recorder_factory else None
        if recorder is not None:
            _, interrupted = await settle(recorder({
                "process_id": identity, "phase": "launching", "host_pid": os.getpid(),
                "host_identity": current_process_identity(), "cwd": str(cwd.resolve()),
                "launch_protocol": "journal-gate-v1",
                "machine_identity": machine_identity(),
                "kind": "windows_job" if os.name == "nt" else "posix_group",
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            }))
            if interrupted:
                await settle(recorder({
                    "process_id": identity, "phase": "launch_failed", "error": "cancelled",
                }))
                raise asyncio.CancelledError

        def spawn() -> ProcessExecution:
            # Ownership transfers to ProcessExecution until its process group is empty.
            output = cast(BinaryIO, tempfile.TemporaryFile())  # noqa: SIM115
            try:
                process: OwnedProcess
                if os.name == "nt":
                    from run_agent_coding.host.windows_jobs import WindowsJobProcess

                    process = WindowsJobProcess(command, cwd, output, identity=identity)
                else:
                    process = PosixProcess(command, cwd, output, bash=bash, identity=identity)
                return ProcessExecution(process, output)
            except BaseException:
                output.close()
                raise

        # A failed native creation retains its durable intent. Recovery must resolve
        # partial creation before considering the associated workspace reusable.
        execution, interrupted = await settle(asyncio.to_thread(spawn))
        self._active[identity] = execution
        try:
            return await self._wait(
                identity, execution, interrupted=interrupted, timeout=timeout,
                cancellation=cancellation, output_limit=output_limit, recorder=recorder,
            )
        finally:
            if identity not in self._active:
                execution.output.close()

    async def _wait(
        self, identity: str, execution: ProcessExecution, *, interrupted: bool,
        timeout: float | None, cancellation: ToolCancellationToken | None, output_limit: int,
        recorder: ProcessRecorder | None,
    ) -> ProcessResult:
        process = execution.process
        start = monotonic()
        timed_out = cancelled = output_limited = False
        try:
            if recorder is not None:
                await recorder({
                    "process_id": identity,
                    "pid": process.pid, "identity": process.identity, "kind": process.kind,
                    "phase": "started", "native_identity": process_identity(process.pid),
                })
            if interrupted or self._closed or (
                cancellation is not None and cancellation.is_cancelled()
            ):
                raise asyncio.CancelledError
            process.resume()
            while process.poll() is None:
                timed_out = timeout is not None and monotonic() - start >= timeout
                cancelled = self._closed or (
                    cancellation is not None and cancellation.is_cancelled()
                )
                output_limited = os.fstat(execution.output.fileno()).st_size > output_limit
                if timed_out or cancelled or output_limited:
                    break
                await asyncio.sleep(0.025)
            output_limited |= os.fstat(execution.output.fileno()).st_size > output_limit
            execution.events.append(
                "timeout" if timed_out else "cancelled" if cancelled
                else "output_limit" if output_limited else "root_exited"
            )
        except asyncio.CancelledError:
            execution.events.append("cancelled")
            raise
        finally:
            _, cleanup_cancelled = await settle(self._drain(identity))
            if recorder is not None:
                _, record_cancelled = await settle(recorder({
                    "process_id": identity,
                    "pid": process.pid, "identity": process.identity, "kind": process.kind,
                    "phase": "exited", "exit_code": execution.exit_code,
                    "events": list(execution.events),
                }))
                cleanup_cancelled |= record_cancelled
            if cleanup_cancelled:
                raise asyncio.CancelledError
        execution.output.seek(0)
        body = execution.output.read(output_limit)
        return ProcessResult(
            body, execution.exit_code, timed_out, cancelled, output_limited,
            process.pid, process.identity, process.kind, tuple(execution.events),
        )

    async def _drain(self, identity: str) -> None:
        async with self._cleanup_lock:
            execution = self._active.get(identity)
            if execution is None:
                return
            process = execution.process
            if process.active_count():
                force = process.kind == "windows_job"
                process.terminate(force=force)
                execution.events.append("kill" if force else "term")
                until = monotonic() + self.grace_period
                while process.active_count() and monotonic() < until:
                    await asyncio.sleep(0.025)
                if process.active_count():
                    process.terminate(force=True)
                    execution.events.append("kill")
            until = monotonic() + self.cleanup_timeout
            while process.active_count() or process.poll() is None:
                if monotonic() >= until:
                    raise ProcessCleanupError(
                        f"Process ownership remains active: {process.identity}"
                    )
                await asyncio.sleep(0.025)
            execution.events.append("empty")
            # Cache the exit code before closing the native process handle.
            code = process.poll()
            execution.exit_code = code
            process.close()
            execution.events.append(f"exit:{code}")
            del self._active[identity]

    async def aclose(self) -> None:
        self._closed = True
        async with self._run_lock:
            for identity in tuple(self._active):
                execution = self._active[identity]
                await self._drain(identity)
                execution.output.close()
