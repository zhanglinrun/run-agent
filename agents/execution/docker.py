"""ExecutionEnvironment adapter over the existing hardened Docker session."""

from __future__ import annotations

from .backend import SandboxBackend, SandboxSession
from .docker_backend import DockerSandboxBackend
from .files import WorkspaceFileOperations
from .models import ExecRequest, SandboxSpec


class DockerExecutionEnvironment(WorkspaceFileOperations):
    def __init__(self, spec: SandboxSpec, *, backend: SandboxBackend | None = None, session: SandboxSession | None = None) -> None:
        super().__init__(spec.workspace)
        self.spec = spec
        self._backend = backend or DockerSandboxBackend()
        self._session = session
        self._last_lifecycle: dict[str, object] = {
            "requested": True,
            "started": session is not None,
            "closed": bool(getattr(session, "closed", False)) if session is not None else False,
            "timed_out": bool(getattr(session, "timed_out", False)) if session is not None else False,
            "container_id": getattr(session, "container_id", None) if session is not None else None,
            "image": spec.image,
            "container_workspace": spec.container_workspace,
        }

    @property
    def session(self) -> SandboxSession | None:
        return self._session

    async def start(self) -> "DockerExecutionEnvironment":
        if self._session is None:
            self._session = await self._backend.start(self.spec)
        self._capture_lifecycle()
        return self

    def _capture_lifecycle(self) -> None:
        if self._session is None:
            return
        snapshot = getattr(self._session, "lifecycle_snapshot", None)
        if callable(snapshot):
            self._last_lifecycle.update(snapshot())
        else:
            self._last_lifecycle.update({
                "started": True,
                "closed": bool(getattr(self._session, "closed", False)),
                "timed_out": bool(getattr(self._session, "timed_out", False)),
                "container_id": getattr(self._session, "container_id", None),
            })

    def lifecycle_snapshot(self) -> dict[str, object]:
        self._capture_lifecycle()
        return dict(self._last_lifecycle)

    @staticmethod
    def shell_argv(command: str) -> tuple[str, ...]:
        return ("bash", "-lc", command)

    async def exec(self, request: ExecRequest):
        if self._session is None:
            await self.start()
        assert self._session is not None
        return await self._session.exec(request)

    async def diff(self) -> str:
        if self._session is None:
            await self.start()
        assert self._session is not None
        return await self._session.export_patch()

    async def close(self) -> None:
        if self._session is not None:
            current = self._session
            try:
                await current.close()
            finally:
                self._capture_lifecycle()
                self._last_lifecycle["closed"] = bool(getattr(current, "closed", False))
                self._last_lifecycle["timed_out"] = bool(getattr(current, "timed_out", False))
                self._session = None
