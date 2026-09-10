"""Bound and constrain the review fork (P5-2).

A review runs a model like anything else, so it needs the discipline Hermes
applies to its background review: an input-token budget that a non-positive value
switches off explicitly, a request count that retries also consume, and at most
one review per principal so a principal cannot multiply its own spending.

It also has to be unable to do harm. The fork may read evidence and propose a
candidate; it may never publish, edit a published asset, touch permission
configuration, or write the main workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

ALLOWED_CAPABILITIES = frozenset({"read_evidence", "create_candidate", "inspect"})


class ReviewCapabilityError(RuntimeError):
    """Raised when the review fork asks for something it must never do."""


@dataclass(frozen=True, slots=True)
class ReviewBudget:
    """One review's ceilings. A non-positive input budget means no cap."""

    max_model_requests: int = 4
    max_input_tokens: int = 20_000
    max_output_tokens: int = 4_000

    @property
    def unlimited_input(self) -> bool:
        """True when the caller explicitly switched the input cap off."""
        return self.max_input_tokens <= 0


class UnattributedUsage(RuntimeError):
    """Raised when a review's spend cannot be attributed to the run that caused it."""


class ReviewLedger:
    """Track what one review spent, and refuse once the budget is gone."""

    def __init__(
        self, budget: ReviewBudget | None = None, *, parent_run_id: str | None = None
    ) -> None:
        self._budget = budget or ReviewBudget()
        self.parent_run_id = parent_run_id
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def attribution(self) -> dict[str, object]:
        """The usage as charged to the run that caused the review.

        A review spends the user's money, so the spend has to belong to someone. An
        anonymous total cannot be explained or acted on, so this refuses rather than
        reporting usage nobody owns.
        """
        if self.parent_run_id is None:
            raise UnattributedUsage(
                "review usage needs a parent run; anonymous spend cannot be attributed"
            )
        return {
            "parent_run_id": self.parent_run_id,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @property
    def budget(self) -> ReviewBudget:
        """The ceilings this ledger enforces."""
        return self._budget

    @property
    def exhausted(self) -> bool:
        """True once the request ceiling has been reached."""
        return self.requests >= self._budget.max_model_requests

    @property
    def remaining_requests(self) -> int:
        """Requests still available, never negative."""
        return max(0, self._budget.max_model_requests - self.requests)

    @property
    def remaining_input_tokens(self) -> int | None:
        """Input tokens still available, or None when uncapped."""
        if self._budget.unlimited_input:
            return None
        return max(0, self._budget.max_input_tokens - self.input_tokens)

    def charge(self, *, requests: int = 1, input_tokens: int = 0, output_tokens: int = 0) -> bool:
        """Record one call and report whether the budget still allowed it.

        The usage is recorded either way, because a refused call that actually
        reached a provider is still spending the user's money.
        """
        allowed = not self.exhausted and self._input_allows(input_tokens)
        self.requests += requests
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        return allowed

    def _input_allows(self, incoming: int) -> bool:
        if self._budget.unlimited_input:
            return True
        return self.input_tokens + incoming <= self._budget.max_input_tokens


@dataclass(frozen=True, slots=True)
class ReviewCapabilities:
    """The closed set of actions a review fork may perform."""

    allowed: frozenset[str] = ALLOWED_CAPABILITIES

    def allows(self, action: str) -> bool:
        """True only for an explicitly allowed action; unknown ones are refused."""
        return action in self.allowed

    def require(self, action: str) -> None:
        """Raise unless the action is allowed."""
        if not self.allows(action):
            raise ReviewCapabilityError(f"the review fork may not {action}")


@dataclass(frozen=True, slots=True)
class ReviewClaim:
    """One principal's exclusive right to be reviewed right now."""

    principal_id: str
    claim_id: str = field(default_factory=lambda: uuid4().hex)


class ReviewWorker:
    """Hands out at most one review claim per principal."""

    def __init__(self) -> None:
        self._claims: dict[str, ReviewClaim] = {}

    def claim(self, principal_id: str) -> ReviewClaim | None:
        """Return a claim, or None when this principal is already being reviewed."""
        if principal_id in self._claims:
            return None
        claim = ReviewClaim(principal_id)
        self._claims[principal_id] = claim
        return claim

    def release(self, claim: ReviewClaim) -> None:
        """Release a claim; a stale or repeated release is an error, not a no-op."""
        current = self._claims.get(claim.principal_id)
        if current is None or current.claim_id != claim.claim_id:
            raise KeyError(f"no active review claim for {claim.principal_id}")
        del self._claims[claim.principal_id]
