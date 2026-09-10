"""Decide which finished runs deserve a review, and how often (P5-1).

A durable completion is not by itself a reason to review. This policy picks out
the runs worth learning from, gives each a stable idempotency key so a duplicate
receipt cannot start a second review, enforces a cooldown, and refuses auxiliary
tasks so that a review can never trigger another review.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

AUXILIARY_ORIGINS = frozenset({"review", "evaluation", "naming"})


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


class ReviewTrigger:
    """Decide whether one completion should start a review."""

    def __init__(
        self,
        policy: ReviewPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or ReviewPolicy()
        self._clock = clock or time.monotonic
        self._reviewed: set[str] = set()
        self._last_admitted_at: float | None = None

    @property
    def policy(self) -> ReviewPolicy:
        """The policy this trigger was built with."""
        return self._policy

    def consider(self, request: ReviewRequest) -> ReviewDecision:
        """Decide once, and remember the decision so a duplicate cannot repeat it."""
        key = self._key(request)
        if request.origin_kind in AUXILIARY_ORIGINS:
            return ReviewDecision(False, "auxiliary task", key)
        if key in self._reviewed:
            return ReviewDecision(False, "already reviewed", key)
        if not self._worth_reviewing(request):
            return ReviewDecision(False, "not worth reviewing", key)
        if self._cooling_down():
            return ReviewDecision(False, "cooling down", key)
        self._reviewed.add(key)
        self._last_admitted_at = self._clock()
        return ReviewDecision(True, "admitted", key)

    def _key(self, request: ReviewRequest) -> str:
        """Idempotency: one run, one review, per policy version."""
        return f"{request.source_run_id}:{self._policy.policy_version}"

    def _worth_reviewing(self, request: ReviewRequest) -> bool:
        """A correction or a failure is worth learning from; chitchat is not."""
        if request.corrections or request.failures:
            return True
        return request.assistant_turns >= self._policy.min_assistant_turns

    def _cooling_down(self) -> bool:
        if self._last_admitted_at is None:
            return False
        return self._clock() - self._last_admitted_at < self._policy.cooldown_seconds
