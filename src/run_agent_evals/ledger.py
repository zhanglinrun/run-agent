"""The cost and latency ledger a report carries.

Chapter 7 lists what model selection has to weigh and, in doing so, what a report has
to report: cost per task, cache and retry behaviour, and the two latency stages it is
easy to conflate - prefill fixes time-to-first-token, decode fixes the rest.

Two of its warnings shape this module. "A cheap model with a low success rate may end
up costing more, because it has to retry often", so attempts are counted and a retry is
never folded into a single call. And p95 tail latency "reflects the real user
experience better than the mean, which a crowd of fast requests drags down while a few
users hit a stall", so the tail is reported next to the mean rather than instead of it.

A cost per success is deliberately absent when nothing succeeded. Zero would read as
free, which is the opposite of the truth.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class EmptyLedger(ValueError):
    """Raised when a summary is asked for with no records to summarise."""


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One physical model call. ``attempt`` is 1 for a first try and higher for a retry."""

    input_tokens: int
    output_tokens: int
    cost: float
    latency_seconds: float
    attempt: int = 1
    time_to_first_token: float | None = None
    succeeded: bool = True


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    """What the report says about cost and latency."""

    calls: int
    retries: int
    successes: int
    total_cost: float
    mean_cost: float
    cost_per_success: float | None
    mean_latency: float
    p95_latency: float
    mean_time_to_first_token: float | None

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "retries": self.retries,
            "successes": self.successes,
            "total_cost": self.total_cost,
            "mean_cost": self.mean_cost,
            "cost_per_success": self.cost_per_success,
            "mean_latency": self.mean_latency,
            "p95_latency": self.p95_latency,
            "mean_time_to_first_token": self.mean_time_to_first_token,
        }


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: no interpolation, so the tail is a real observation."""
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, -(-int(fraction * len(ordered)) - 1)))
    return ordered[index]


def summarise(records: Iterable[CallRecord]) -> LedgerSummary:
    """Reduce call records to the figures the report carries."""
    entries = tuple(records)
    if not entries:
        raise EmptyLedger("cannot summarise a ledger with no records")
    latencies = [entry.latency_seconds for entry in entries]
    costs = [entry.cost for entry in entries]
    successes = sum(1 for entry in entries if entry.succeeded)
    first_tokens = [
        entry.time_to_first_token for entry in entries if entry.time_to_first_token is not None
    ]
    return LedgerSummary(
        calls=len(entries),
        retries=sum(1 for entry in entries if entry.attempt > 1),
        successes=successes,
        total_cost=sum(costs),
        mean_cost=sum(costs) / len(costs),
        cost_per_success=(sum(costs) / successes) if successes else None,
        mean_latency=sum(latencies) / len(latencies),
        p95_latency=percentile(latencies, 0.95),
        mean_time_to_first_token=(sum(first_tokens) / len(first_tokens) if first_tokens else None),
    )
