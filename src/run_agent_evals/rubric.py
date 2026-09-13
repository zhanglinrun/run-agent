"""A weighted rubric with pitfalls and a veto.

Chapter 7's four rules, and what each one demands here:

  grounded in expert guidance  the scoring tiers carry the domain judgement, so they
                               are required rather than optional
  comprehensive coverage       pitfalls are a weight of their own, not the absence of
                               a positive dimension
  weighted by importance       essential, important, optional, pitfall, with a veto
  self-contained               each tier is a concrete statement, checked non-empty

The veto is the part worth being careful about. Chapter 7 introduces it because some
failures cannot be outscored: a fabricated record is not a mediocre answer that good
scores elsewhere can compensate for. So a veto is recorded separately from the mean
and cannot be averaged away.

Scoring is arithmetic over ratings. Chapter 7 is explicit that anything expressible as
a programmatic assertion should stay one and model judgement is only for dimensions
that cannot be decided mechanically, so a weighted mean is computed here rather than
asked of a judge. Ratings themselves are the caller's input, which is where a judge
belongs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

WEIGHTS = ("essential", "important", "optional", "pitfall")
WEIGHT_VALUES = MappingProxyType(
    {"essential": 3.0, "important": 2.0, "optional": 1.0, "pitfall": 3.0}
)
VETO_WEIGHT = "pitfall"


class UnknownRating(KeyError):
    """Raised when a rated rubric is missing a dimension's rating."""


@dataclass(frozen=True, slots=True)
class Dimension:
    """One scored dimension: its weight, its tiers, and any veto threshold."""

    name: str
    weight: str
    scoring: Mapping[int, str]
    veto_below: int | None = None

    def __post_init__(self) -> None:
        if self.weight not in WEIGHT_VALUES:
            raise ValueError(f"unknown weight {self.weight!r}; expected one of {WEIGHTS}")
        if not self.scoring:
            raise ValueError(f"dimension {self.name!r} needs scoring tiers to be self-contained")
        if self.veto_below is not None and self.weight != VETO_WEIGHT:
            raise ValueError(
                f"a veto belongs to a {VETO_WEIGHT} dimension; {self.name!r} is {self.weight!r}"
            )


@dataclass(frozen=True, slots=True)
class Rubric:
    """The dimensions a judgement is made against."""

    dimensions: tuple[Dimension, ...]

    def __post_init__(self) -> None:
        names = [dimension.name for dimension in self.dimensions]
        if len(set(names)) != len(names):
            raise ValueError(f"dimension names must be unique: {names}")


@dataclass(frozen=True, slots=True)
class RubricScore:
    """The weighted mean, plus which dimension vetoed the result if any."""

    weighted: float
    vetoed_by: str | None = None

    @property
    def accepted(self) -> bool:
        return self.vetoed_by is None


def score(rubric: Rubric, ratings: Mapping[str, int]) -> RubricScore:
    """Average the ratings by weight, recording any veto separately.

    A veto does not lower the mean; it disqualifies the result. Reporting both keeps
    the arithmetic honest while refusing to let a fabricated answer outscore a
    merely thin one.
    """
    total = 0.0
    weight_sum = 0.0
    vetoed_by: str | None = None
    for dimension in rubric.dimensions:
        if dimension.name not in ratings:
            raise UnknownRating(f"missing rating for {dimension.name!r}")
        rating = ratings[dimension.name]
        weight = WEIGHT_VALUES[dimension.weight]
        total += rating * weight
        weight_sum += weight
        if dimension.veto_below is not None and rating < dimension.veto_below:
            vetoed_by = vetoed_by or dimension.name
    return RubricScore(weighted=total / weight_sum, vetoed_by=vetoed_by)
