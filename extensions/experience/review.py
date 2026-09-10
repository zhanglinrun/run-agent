"""Decide which finished runs deserve a review, and how often (P5-1).

A durable completion is not by itself a reason to review. This policy picks out
the runs worth learning from, gives each a stable idempotency key so a duplicate
receipt cannot start a second review, enforces a cooldown, and refuses auxiliary
tasks so that a review can never trigger another review.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import ExtensionAPI, ExtensionContext
from run_agent_coding.host.contracts import StateChange
from run_agent_core.types import JSONValue

from .worker import ReviewWorker

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


class ReviewCoordinator:
    """Turn an admitted durable completion into one durable review request.

    The review request is the hand-off to the isolated worker: it is written to
    the extension's own namespace, so a later worker can pick it up without the
    completion path waiting for a model call.
    """

    def __init__(self, api: ExtensionAPI, trigger: ReviewTrigger | None = None) -> None:
        self._api = api
        self._trigger = trigger or ReviewTrigger()
        self._worker = ReviewWorker()

    async def settled(self, event: object, context: ExtensionContext) -> None:
        """Record at most one review request for an admitted completion."""
        del context
        if not isinstance(event, AgentSettledEvent):
            return
        decision = self._trigger.consider(self._request(event))
        if not decision.admitted:
            return
        key = f"{REVIEW_REQUEST_PREFIX}{event.run_id}"
        state = self._api.context.services.scope("session").state
        if await state.get(key) is not None:
            return
        await state.compare_and_set(
            StateChange(
                key,
                0,
                {"run_id": event.run_id, "key": decision.key, "status": event.status},
            )
        )

    def _request(self, event: AgentSettledEvent) -> ReviewRequest:
        return ReviewRequest(
            source_run_id=event.run_id,
            session_id=event.session_id,
            status=event.status,
            assistant_turns=1,
            corrections=0,
            failures=0 if event.status == "succeeded" else 1,
        )

    async def consume(self, payload: object, task_context: object) -> JSONValue:
        """Consume one queued review request under the review worker's claim."""
        del task_context
        run_id = str(payload.get("run_id") or "") if isinstance(payload, Mapping) else ""
        if not run_id:
            raise ValueError("experience-review needs a run_id")
        # A session belongs to one principal, so claiming per session is the
        # stricter form of the worker's one-review-per-principal rule.
        claim = self._worker.claim(self._api.context.session_id or "session")
        if claim is None:
            return {"consumed": None, "reason": "a review is already running"}
        try:
            return await self._consume_once(run_id)
        finally:
            self._worker.release(claim)

    async def _consume_once(self, run_id: str) -> JSONValue:
        state = self._api.context.services.scope("session").state
        pending = await state.get(f"{REVIEW_REQUEST_PREFIX}{run_id}")
        consumed_key = f"review-consumed:{run_id}"
        if pending is None or await state.get(consumed_key) is not None:
            return {"consumed": None}
        await state.compare_and_set(StateChange(consumed_key, 0, {"run_id": run_id}))
        key = pending.value.get("key") if isinstance(pending.value, Mapping) else None
        return {"consumed": run_id, "key": key}
