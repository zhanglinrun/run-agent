"""Evaluation metrics as chapter 7 of ai-agent-book defines them.

Chapter 7 fixes the arithmetic rather than leaving it to taste:

    Pass@k = 1 - (1 - p)^k        Pass^k = p^k        SE(p) ~ sqrt(p (1 - p) / n)

The two are answers to different questions. ``pass_at_k`` is capability reach - at
least one of k attempts succeeds - and belongs to exploration. ``pass_power_k`` is
reliability - every one of k consecutive attempts succeeds - and is what payment,
refund and deployment decisions actually need. Chapter 7 also requires the report
to say which sense of k was meant, so both docstrings state their reading.

Reported differences between two configurations must be paired, because a few
points on a few dozen cases is sampling noise: the interval shrinks as 1/sqrt(n),
so ``trials_for_margin`` answers "how many cases before this difference means
anything" instead of letting a 3-point gap drive a decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil, comb, sqrt
from statistics import NormalDist

DEFAULT_LEVEL = 0.95


def _z(level: float) -> float:
    """Two-sided normal quantile for a confidence level."""
    return NormalDist().inv_cdf(1 - (1 - level) / 2)


def pass_at_k(rate: float, k: int) -> float:
    """At-least-one sense: k independent attempts on the same task, any one succeeds."""
    return 1 - (1 - rate) ** k


def pass_power_k(rate: float, k: int) -> float:
    """Consecutive sense: k attempts in a row, every one must succeed (Pass^k)."""
    return rate**k


def standard_error(rate: float, trials: int) -> float:
    """Chapter 7's approximate standard error of an observed success rate."""
    if trials <= 0:
        raise ValueError("A success rate needs a positive number of trials")
    return sqrt(rate * (1 - rate) / trials)


def confidence_interval(
    rate: float, trials: int, level: float = DEFAULT_LEVEL
) -> tuple[float, float]:
    """Normal-approximation interval for an observed rate."""
    half = _z(level) * standard_error(rate, trials)
    return rate - half, rate + half


def trials_for_margin(margin: float, rate: float, level: float = DEFAULT_LEVEL) -> int:
    """Cases needed before a difference of ``margin`` is resolvable.

    Answers chapter 7's warning that a 2-3 point gap on a few dozen cases should
    send you to collect more data rather than to switch models.
    """
    if margin <= 0:
        raise ValueError("A margin must be positive")
    return ceil(rate * (1 - rate) * _z(level) ** 2 / margin**2)


@dataclass(frozen=True)
class Ratio:
    """A success count over a trial count, with its uncertainty."""

    successes: int
    trials: int

    def __post_init__(self) -> None:
        if self.trials <= 0:
            raise ValueError("A success rate needs a positive number of trials")

    @classmethod
    def of(cls, outcomes: Sequence[bool]) -> Ratio:
        return cls(sum(1 for outcome in outcomes if outcome), len(outcomes))

    @property
    def rate(self) -> float:
        return self.successes / self.trials

    @property
    def standard_error(self) -> float:
        return standard_error(self.rate, self.trials)

    def interval(self, level: float = DEFAULT_LEVEL) -> tuple[float, float]:
        return confidence_interval(self.rate, self.trials, level)


@dataclass(frozen=True)
class PairedComparison:
    """Per-task paired outcome counts between two configurations.

    ``left_only`` counts tasks only the left configuration solved and ``right_only``
    the mirror; those two discordant cells are the entire evidence, because tasks
    both sides solved (or both missed) say nothing about which is better.
    """

    left_only: int
    right_only: int
    agreed_pass: int
    agreed_fail: int

    @classmethod
    def of(cls, left: Sequence[bool], right: Sequence[bool]) -> PairedComparison:
        if len(left) != len(right):
            raise ValueError("A paired comparison needs the same tasks on both sides")
        both = list(zip(left, right, strict=True))
        return cls(
            left_only=sum(1 for a, b in both if a and not b),
            right_only=sum(1 for a, b in both if b and not a),
            agreed_pass=sum(1 for a, b in both if a and b),
            agreed_fail=sum(1 for a, b in both if not a and not b),
        )

    @property
    def total(self) -> int:
        return self.left_only + self.right_only + self.agreed_pass + self.agreed_fail

    @property
    def difference(self) -> float:
        return (self.left_only - self.right_only) / self.total

    @property
    def exact_p_value(self) -> float:
        """Two-sided exact McNemar test over the discordant pairs."""
        discordant = self.left_only + self.right_only
        if discordant == 0:
            return 1.0
        tail = min(self.left_only, self.right_only)
        smaller = sum(comb(discordant, i) for i in range(tail + 1)) / 2**discordant
        return min(1.0, 2 * float(smaller))

    def is_significant(self, level: float = 0.05) -> bool:
        return self.exact_p_value < level


def observed_at_least_one(attempts: Iterable[Sequence[bool]]) -> Ratio:
    """Per-task share with at least one success across that task's attempts."""
    rows = [list(row) for row in attempts]
    return Ratio(sum(1 for row in rows if any(row)), len(rows))


def observed_all_passed(attempts: Iterable[Sequence[bool]]) -> Ratio:
    """Per-task share where every attempt succeeded."""
    rows = [list(row) for row in attempts]
    return Ratio(sum(1 for row in rows if row and all(row)), len(rows))


@dataclass(frozen=True)
class SeedSummary:
    """A metric across seeds, reported as a mean with its observed range.

    Chapter 7 asks for 3-5 seeds and the spread, because a single run is only good
    for choosing a direction.
    """

    mean: float
    low: float
    high: float
    per_seed: tuple[float, ...]


def summarize_seeds(rates: Sequence[float]) -> SeedSummary:
    if not rates:
        raise ValueError("A seed summary needs at least one seed")
    return SeedSummary(
        mean=sum(rates) / len(rates),
        low=min(rates),
        high=max(rates),
        per_seed=tuple(rates),
    )
