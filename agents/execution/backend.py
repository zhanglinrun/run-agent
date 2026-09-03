"""Protocols for sandbox execution backends."""

from __future__ import annotations

from typing import Protocol

from .models import ExecRequest, ExecResult, SandboxSpec


class SandboxSession(Protocol):
    spec: SandboxSpec

    async def exec(self, request: ExecRequest) -> ExecResult: ...

    async def export_patch(self) -> str: ...

    async def close(self) -> None: ...


class SandboxBackend(Protocol):
    async def start(self, spec: SandboxSpec) -> SandboxSession: ...


__all__ = ["SandboxBackend", "SandboxSession"]
