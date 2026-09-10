"""Character budgets and time-based curation for experience assets (T-023).

Aligned with the Hermes reference in docs/implementation/hermes-alignment.md.

Budgets are counted in characters rather than tokens because the constraint being
modelled is what fits in a prompt, and Hermes uses the same unit for the same reason.
An over-limit write is refused with its usage rather than truncated: silently dropping
the tail of a memory is worse than telling the caller it did not fit.

Curation is a time transition, not a judgement call. An asset nobody has used for
thirty days is stale and one unused for ninety is archive-eligible, which is how a
store stops growing forever without anyone deciding to delete things. A pinned asset
is exempt from both, because automatic maintenance must never remove what the user
chose to keep - the same principle the write provenance already enforces for skills.

A dry run returns the same decision with ``applied`` false, so asking what would
happen is answerable without anything happening.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

# The Hermes reference limits, in characters.
USER_CHAR_LIMIT = 1375
MEMORY_CHAR_LIMIT = 2200

STALE_AFTER = timedelta(days=30)
ARCHIVE_AFTER = timedelta(days=90)


class CurationAction(Enum):
    """What automatic maintenance would do to an asset."""

    KEEP = "keep"
    STALE = "stale"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """How much of a character budget is spoken for."""

    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class BudgetExceeded(ValueError):
    """Raised when a write would push a character budget past its limit."""

    def __init__(self, usage: BudgetUsage) -> None:
        super().__init__(
            f"character budget exceeded: {usage.used} of {usage.limit} characters in use"
        )
        self.usage = usage


@dataclass(frozen=True, slots=True)
class CharacterBudget:
    """A character budget for one scope, such as USER or MEMORY."""

    limit: int

    def reserve(self, addition: str, *, existing: Iterable[str] = ()) -> BudgetUsage:
        """Account for a write, refusing it if the result would not fit."""
        used = sum(len(entry) for entry in existing) + len(addition)
        usage = BudgetUsage(used=used, limit=self.limit)
        if used > self.limit:
            raise BudgetExceeded(usage)
        return usage


@dataclass(frozen=True, slots=True)
class CurationDecision:
    """The action for one asset, and whether it was carried out."""

    action: CurationAction
    reason: str
    applied: bool


def curate(
    *,
    last_used: datetime,
    now: datetime,
    pinned: bool,
    dry_run: bool = False,
) -> CurationDecision:
    """Decide what happens to an asset that has gone unused for a while."""
    if pinned:
        return CurationDecision(CurationAction.KEEP, "pinned assets are never curated", False)
    idle = now - last_used
    applied = not dry_run
    if idle >= ARCHIVE_AFTER:
        days = ARCHIVE_AFTER.days
        return CurationDecision(CurationAction.ARCHIVE, f"unused for {days} days or more", applied)
    if idle >= STALE_AFTER:
        days = STALE_AFTER.days
        return CurationDecision(CurationAction.STALE, f"unused for {days} days or more", applied)
    return CurationDecision(CurationAction.KEEP, "recently used", False)
