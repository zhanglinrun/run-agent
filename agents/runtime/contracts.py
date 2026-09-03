"""Stable data contracts shared by the runtime, traces and evaluation.

The original project passed loosely-shaped dictionaries through most of the
runtime.  That is convenient while prototyping, but it makes it difficult to
replay a run or explain a benchmark failure.  These small, dependency-free
dataclasses are the boundary between the agent loop and the evidence layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


class EventType(str, Enum):
    RUN_STARTED = "run.started"
    USER_MESSAGE = "user.message"
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    TOOL_REQUESTED = "tool.requested"
    TOOL_EFFECTIVE = "tool.effective"
    PERMISSION_DECISION = "permission.decision"
    TOOL_RESULT = "tool.result"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    ok: bool
    duration_ms: float = 0.0
    error: str | None = None
    executed: bool = True


@dataclass
class EvalCase:
    """A benchmark task with explicit correctness and process expectations."""

    case_id: str
    prompt: str
    expected: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    difficulty: str = "unknown"
    source: str = "local"

    @classmethod
    def from_dict(cls, value: dict[str, Any], index: int = 0) -> "EvalCase":
        case_id = str(value.get("id") or value.get("case_id") or f"case-{index + 1}")
        return cls(
            case_id=case_id,
            prompt=str(value.get("prompt") or value.get("question") or ""),
            expected=dict(value.get("expected") or {}),
            tags=[str(item) for item in value.get("tags", [])],
            difficulty=str(value.get("difficulty") or "unknown"),
            source=str(value.get("source") or "local"),
        )


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    score: float
    correctness: float
    process: float
    safety: float
    checks: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
