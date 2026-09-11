"""The value types a review decision is made from and expressed as.

Extracted from ``review`` once that module crossed 200 lines: the policy, the evidence a
completion carries, the decision, and the two namespace constants are data, while the
trigger and coordinator are behaviour. Keeping them together meant the file grew every
time either half gained a field.
"""

from __future__ import annotations

from dataclasses import dataclass

# A review must never be triggered by another review, by the evaluating path, or by the
# naming path: those are auxiliary work, not a user turn worth learning from.
AUXILIARY_ORIGINS = frozenset({"review", "evaluation", "naming"})
REVIEW_REQUEST_PREFIX = "review-request:"


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    """Startup configuration; versioned so a policy change may re-review a run."""

    policy_version: str = "1"
    cooldown_seconds: float = 900.0
    min_assistant_turns: int = 2


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    """The evidence a durable completion carries into the trigger."""

    source_run_id: str
    session_id: str
    status: str
    assistant_turns: int
    corrections: int
    failures: int
    origin_kind: str = "user"


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Whether to review, why not when not, and the idempotency key."""

    admitted: bool
    reason: str
    key: str
