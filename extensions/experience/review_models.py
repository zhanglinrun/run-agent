"""The value types a review decision is made from and expressed as.

Extracted from ``review`` once that module crossed 200 lines: the policy, the evidence a
completion carries, the decision, and the two namespace constants are data, while the
trigger and coordinator are behaviour. Keeping them together meant the file grew every
time either half gained a field.
"""

from __future__ import annotations

from collections.abc import Callable
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


@dataclass(frozen=True, slots=True)
class ForegroundGate:
    """Whether a foreground run is in flight, so a review can yield to it.

    The gate belongs at the point where a review would start, not where one is queued: a
    completion event arrives while its own session is still running, so checking the
    foreground while queueing would defer every review forever.

    It is fed the application's ``is_running``, measured rather than assumed: a probe
    around a real foreground run reports false before start, after start, and after the
    prompt returns, for a succeeding provider and a failing one alike. So the signal means
    "a run is in flight", which is exactly what the gate needs.
    """

    busy: Callable[[], bool]

    def deferral(self) -> str | None:
        """The reason to hold the review back, or ``None`` to proceed."""
        return "foreground busy" if self.busy() else None
