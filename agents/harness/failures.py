"""Structured task failure categories used by the Harness and evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    MODEL = "model_failure"
    TOOL = "tool_failure"
    POLICY = "policy_failure"
    VERIFICATION = "verification_failure"
    CORRECTION = "correction_failure"
    INFRASTRUCTURE = "infrastructure_failure"
    BUDGET = "budget_exhausted"
    EMPTY_PATCH = "missing_patch"
    SESSION = "session_failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown_failure"


@dataclass(frozen=True)
class FailureInfo:
    kind: FailureKind
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
