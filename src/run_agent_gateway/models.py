"""Turn scheduling protocol shared by gateways and coding-session adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from uuid import uuid4

from run_agent_core.types import JSONValue

TurnLane = Literal["foreground", "background"]
TurnStatus = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class TurnRequest:
    session_id: str
    content: str
    id: str = field(default_factory=lambda: uuid4().hex)
    lane: TurnLane = "foreground"
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    submitted_at: float = field(default_factory=monotonic)


@dataclass(frozen=True, slots=True)
class TurnResult:
    request_id: str
    session_id: str
    status: TurnStatus
    output: str = ""
    error: str | None = None
    submitted_at: float | None = None
    started_at: float | None = None
    finished_at: float = field(default_factory=monotonic)
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    @property
    def queue_latency_ms(self) -> float | None:
        if self.started_at is None or self.submitted_at is None:
            return None
        return max(0.0, self.started_at - self.submitted_at) * 1000

    @classmethod
    def succeeded(
        cls,
        request: TurnRequest,
        *,
        output: str,
        started_at: float | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> TurnResult:
        return cls(
            request_id=request.id,
            session_id=request.session_id,
            status="succeeded",
            output=output,
            submitted_at=request.submitted_at,
            started_at=started_at,
            metadata=metadata or {},
        )

    @classmethod
    def failed(
        cls,
        request: TurnRequest,
        *,
        error: str,
        output: str = "",
        started_at: float | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> TurnResult:
        return cls(
            request_id=request.id,
            session_id=request.session_id,
            status="failed",
            output=output,
            error=error,
            submitted_at=request.submitted_at,
            started_at=started_at,
            metadata=metadata or {},
        )

    @classmethod
    def cancelled(
        cls,
        request: TurnRequest,
        *,
        started_at: float | None = None,
    ) -> TurnResult:
        return cls(
            request_id=request.id,
            session_id=request.session_id,
            status="cancelled",
            error="turn cancelled",
            submitted_at=request.submitted_at,
            started_at=started_at,
        )
