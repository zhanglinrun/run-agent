"""Deterministic execution fakes used only by tests."""

from __future__ import annotations

from agents.execution import ExecRequest, ExecResult, SandboxSpec


class FakeSandboxSession:
    def __init__(
        self,
        spec: SandboxSpec,
        *,
        results: list[ExecResult] | None = None,
        patch: str = "",
    ) -> None:
        self.spec = spec
        self.results = list(results or [])
        self.patch = patch
        self.requests: list[ExecRequest] = []
        self.closed = False

    async def exec(self, request: ExecRequest) -> ExecResult:
        self.requests.append(request)
        if self.results:
            return self.results.pop(0)
        return ExecResult(request.argv, 0, "", "", False, 0.0)

    async def export_patch(self) -> str:
        if self.closed:
            raise RuntimeError("fake sandbox is closed")
        return self.patch

    async def close(self) -> None:
        self.closed = True


class FakeSandboxBackend:
    def __init__(
        self,
        *,
        results: list[ExecResult] | None = None,
        patch: str = "",
    ) -> None:
        self.results = results or []
        self.patch = patch
        self.sessions: list[FakeSandboxSession] = []

    async def start(self, spec: SandboxSpec) -> FakeSandboxSession:
        session = FakeSandboxSession(spec, results=self.results, patch=self.patch)
        self.sessions.append(session)
        return session
