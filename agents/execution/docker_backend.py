"""Docker implementation of the task sandbox contract."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import subprocess
import time
import uuid

from .errors import SandboxInfrastructureError, SandboxUnavailableError, SandboxWorkspaceError
from .models import ExecRequest, ExecResult, SandboxSpec


def _trim(value: str, limit: int = 200_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n[... {len(value) - limit} chars omitted ...]"


def _run_docker(argv: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False, check=False,
        )
    except FileNotFoundError as exc:
        raise SandboxUnavailableError("Docker CLI is not installed or not on PATH") from exc


class DockerSandboxSession:
    def __init__(self, spec: SandboxSpec, container_id: str, *, docker_binary: str = "docker") -> None:
        self.spec = spec
        self.container_id = container_id
        self.docker_binary = docker_binary
        self._closed = False
        self._started = time.monotonic()
        self.timed_out = False

    @property
    def closed(self) -> bool:
        return self._closed

    def lifecycle_snapshot(self) -> dict[str, object]:
        return {
            "started": True,
            "closed": self._closed,
            "timed_out": self.timed_out,
            "container_id": self.container_id,
            "image": self.spec.image,
            "container_workspace": self.spec.container_workspace,
        }

    def _remaining(self, requested: float) -> float:
        remaining = self.spec.timeout_seconds - (time.monotonic() - self._started)
        if remaining <= 0:
            raise SandboxInfrastructureError("sandbox task timeout exceeded")
        return min(requested, remaining)

    async def exec(self, request: ExecRequest) -> ExecResult:
        if self._closed:
            raise SandboxInfrastructureError("cannot execute in a closed sandbox")
        try:
            timeout = self._remaining(request.timeout_seconds)
        except SandboxInfrastructureError:
            self.timed_out = True
            await self.close()
            raise
        argv = [self.docker_binary, "exec", "--workdir", request.cwd]
        secret_key = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie|credential)")
        for key, value in sorted(request.env.items()):
            if not secret_key.search(key):
                argv.extend(["--env", f"{key}={value}"])
        argv.extend([self.container_id, *request.argv])
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(_run_docker, argv, timeout=timeout)
            return ExecResult(
                argv=request.argv,
                exit_code=result.returncode,
                stdout=_trim(result.stdout or ""),
                stderr=_trim(result.stderr or ""),
                timed_out=False,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except subprocess.TimeoutExpired as exc:
            self.timed_out = True
            return ExecResult(
                argv=request.argv,
                exit_code=None,
                stdout=_trim(str(exc.stdout or "")),
                stderr=_trim(str(exc.stderr or "")),
                timed_out=True,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
        except SandboxInfrastructureError:
            raise
        except Exception as exc:
            await self.close()
            raise SandboxInfrastructureError(f"docker exec failed: {exc}") from exc

    async def export_patch(self) -> str:
        patch_timeout = self.spec.patch_timeout_seconds
        untracked = await self.exec(ExecRequest(
            ("git", "ls-files", "--others", "--exclude-standard"),
            cwd=self.spec.container_workspace,
            timeout_seconds=patch_timeout,
        ))
        if not untracked.ok:
            raise SandboxInfrastructureError(
                f"patch preparation failed (exit={untracked.exit_code}): {untracked.stderr or untracked.stdout}"
            )
        ignored_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"}
        paths = tuple(
            line.strip() for line in untracked.stdout.splitlines()
            if line.strip()
            and not ignored_parts.intersection(Path(line.strip()).parts)
            and Path(line.strip()).suffix not in {".pyc", ".pyo"}
            and Path(line.strip()).name != ".coverage"
        )
        result = await self.exec(ExecRequest(
            ("git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-color"),
            cwd=self.spec.container_workspace,
            timeout_seconds=patch_timeout,
        ))
        if not result.ok:
            raise SandboxInfrastructureError(
                f"patch export failed (exit={result.exit_code}): {result.stderr or result.stdout}"
            )
        additions: list[str] = []
        for path in paths:
            added = await self.exec(ExecRequest(
                ("git", "diff", "--no-index", "--binary", "--no-ext-diff", "--", "/dev/null", path),
                cwd=self.spec.container_workspace,
                timeout_seconds=patch_timeout,
            ))
            if added.exit_code not in {0, 1} or added.timed_out:
                raise SandboxInfrastructureError(
                    f"untracked patch export failed for {path} (exit={added.exit_code}): {added.stderr or added.stdout}"
                )
            additions.append(added.stdout)
        return result.stdout + "".join(additions)

    async def close(self) -> None:
        if self._closed:
            return
        cleanup_errors: list[str] = []
        stop_command = [self.docker_binary, "stop", "--time", "2", self.container_id]
        remove_command = [self.docker_binary, "rm", "--force", self.container_id]
        try:
            await asyncio.to_thread(_run_docker, stop_command, timeout=15.0)
        except Exception as exc:
            cleanup_errors.append(f"stop: {exc}")
        try:
            removed = await asyncio.to_thread(_run_docker, remove_command, timeout=15.0)
            message = (removed.stderr or removed.stdout or "").lower()
            if removed.returncode != 0 and "no such container" not in message:
                cleanup_errors.append(f"remove exit={removed.returncode}: {message[-1000:]}")
            else:
                self._closed = True
        except Exception as exc:
            cleanup_errors.append(f"remove: {exc}")
        if not self._closed:
            raise SandboxInfrastructureError(
                "sandbox cleanup failed; container may remain: " + "; ".join(cleanup_errors)
            )


class DockerSandboxBackend:
    def __init__(self, *, docker_binary: str = "docker") -> None:
        self.docker_binary = docker_binary

    async def start(self, spec: SandboxSpec) -> DockerSandboxSession:
        workspace = Path(spec.workspace).resolve()
        if not workspace.is_dir():
            raise SandboxWorkspaceError(f"sandbox workspace is not a directory: {workspace}")
        labels = {
            "run-agent.run_id": spec.run_id or uuid.uuid4().hex,
            "run-agent.case_id": spec.case_id or "unknown",
        }
        container_name = f"run-agent-{uuid.uuid4().hex[:12]}"
        container_workspace = spec.container_workspace
        command = [
            self.docker_binary, "run", "--init", "--detach", "--name", container_name,
            "--label", f"run-agent.run_id={labels['run-agent.run_id']}",
            "--label", f"run-agent.case_id={labels['run-agent.case_id']}",
            "--network", spec.network, "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            "--mount", f"type=bind,source={workspace},target={container_workspace}",
            "--workdir", container_workspace,
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", f"{spec.memory_mb}m", "--cpus", str(spec.cpus), "--pids-limit", str(spec.pids_limit),
            "--env", "HOME=/tmp",
            "--env", "GIT_CONFIG_COUNT=1",
            "--env", "GIT_CONFIG_KEY_0=safe.directory",
            "--env", f"GIT_CONFIG_VALUE_0={container_workspace}",
            spec.image, "sleep", "infinity",
        ]
        try:
            # The first task for a SWE-bench image may need to pull and unpack
            # a several-hundred-megabyte image.  A short CLI timeout turns a
            # healthy daemon into a false ``SandboxUnavailableError`` during
            # that cold-start path, so keep startup bounded by the sandbox
            # task timeout while retaining a conservative upper limit.
            startup_timeout = min(max(float(spec.timeout_seconds), 30.0), 900.0)
            result = await asyncio.to_thread(_run_docker, command, timeout=startup_timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                await asyncio.to_thread(_run_docker, [self.docker_binary, "rm", "--force", container_name], timeout=15.0)
            except Exception:
                pass
            raise SandboxUnavailableError("Docker daemon did not respond while starting the sandbox") from exc
        except OSError as exc:
            raise SandboxUnavailableError(f"Docker daemon unavailable: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Docker daemon unavailable").strip()
            lowered = message.lower()
            if "daemon" in lowered or "cannot connect" in lowered or "pipe" in lowered:
                raise SandboxUnavailableError(f"Docker daemon unavailable: {message[-1000:]}")
            raise SandboxInfrastructureError(f"docker run failed (exit={result.returncode}): {message[-2000:]}")
        container_id = (result.stdout or "").strip().splitlines()[0] if result.stdout else ""
        if not container_id:
            raise SandboxInfrastructureError("docker run returned no container id")
        return DockerSandboxSession(spec, container_id, docker_binary=self.docker_binary)


__all__ = ["DockerSandboxBackend", "DockerSandboxSession"]
