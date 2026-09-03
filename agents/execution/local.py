"""Local execution environment used by interactive sessions and tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import time

from .files import WorkspaceFileOperations
from .models import ExecRequest, ExecResult
from ..runtime.scope import current_workspace
from .workspace import git_diff


class LocalExecutionEnvironment(WorkspaceFileOperations):
    def __init__(self, workspace_root: str | Path | None = None) -> None:
        super().__init__(workspace_root or current_workspace())

    def shell_argv(self, command: str) -> tuple[str, ...]:
        if os.name == "nt":
            return ("powershell", "-NoProfile", "-Command", command)
        return ("bash", "-lc", command)

    async def exec(self, request: ExecRequest) -> ExecResult:
        started = time.perf_counter()
        raw_cwd = request.cwd
        if raw_cwd == "/workspace" or raw_cwd.startswith("/workspace/"):
            raw_cwd = str(self.workspace_root) + raw_cwd[len("/workspace"):]
        cwd = self._resolve(raw_cwd)
        import re
        secret_key = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie|credential)")
        child_env = {key: value for key, value in os.environ.items() if not secret_key.search(key)}
        child_env.update(request.env)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                list(request.argv),
                cwd=str(cwd),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds,
                shell=False,
            )
            return ExecResult(request.argv, result.returncode, result.stdout or "", result.stderr or "", False, (time.perf_counter() - started) * 1000)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(request.argv, None, str(exc.stdout or ""), str(exc.stderr or ""), True, (time.perf_counter() - started) * 1000)

    async def diff(self) -> str:
        return await asyncio.to_thread(git_diff, self.workspace_root)

    async def close(self) -> None:
        return None
