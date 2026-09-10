"""Turn an attributed failure into a regression task (T-016, T-017).

Chapter 7 closes the loop here: once the first error and its category are known, the
dataset can be extended with end-to-end and trajectory-prefix regression tasks, and it
prescribes the shape for each category. This module encodes that prescription rather
than leaving it to taste - a missing process step becomes an end-to-end task carrying
acceptance conditions, an ambiguous requirement becomes a prefix whose acceptable
actions include asking first, and a verification-fraud failure becomes a prefix with
two hard constraints against editing assertions and claiming completion without the
command output that was actually run.

It also holds the two rules that decide whether the resulting number means anything.
Material that was learned from must not sit inside the retained set, or the score
measures recall of the thing that was just taught. And the report must be rebuildable
from stored evidence, because a report that can only be produced by running everything
again cannot be audited.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from run_agent_evals.attribution import Attribution

ORIGINS = ("public_benchmark", "self_built", "production_trajectory")
ORIGIN_SET = frozenset(ORIGINS)

END_TO_END = "end_to_end"
TRAJECTORY_PREFIX = "trajectory_prefix"

ASK_FIRST = "ask first, do not guess"
DO_NOT_EDIT_ASSERTIONS = "do not modify the test assertions"
CARRY_COMMAND_OUTPUT = "a completion claim must carry the command output that was run"

CONSTRUCTION: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "流程缺失": (END_TO_END, ("carry a plan document", "carry acceptance conditions")),
    "工具调用错误": (
        TRAJECTORY_PREFIX,
        ("truncate the failing prefix", "correct the format, escaping, or tool choice"),
    ),
    "执行异常": (
        TRAJECTORY_PREFIX,
        ("add a truncation recovery scenario", "add a timeout and tool-failure scenario"),
    ),
    "完成度与逻辑错误": (
        END_TO_END,
        ("carry a multi-goal checklist", "carry a remaining-tasks reminder"),
    ),
    "需求理解与歧义": (
        TRAJECTORY_PREFIX,
        (ASK_FIRST, "freeze the ambiguous request as the prefix"),
    ),
    "症状修复与验证造假": (
        TRAJECTORY_PREFIX,
        (DO_NOT_EDIT_ASSERTIONS, CARRY_COMMAND_OUTPUT),
    ),
    "信息反馈": (END_TO_END, ("assert on the reply content itself",)),
}


class LearnedMaterialInHoldout(ValueError):
    """Raised when material that was learned from is also being measured."""


@dataclass(frozen=True, slots=True)
class RegressionTask:
    """A task derived from one attributed failure."""

    task_id: str
    kind: str
    origin: str
    from_category: str
    constraints: tuple[str, ...]


def reflow(attribution: Attribution, *, task_id: str, origin: str) -> RegressionTask:
    """Build the regression task chapter 7 prescribes for this failure category."""
    if origin not in ORIGIN_SET:
        raise ValueError(f"unknown origin {origin!r}; expected one of {ORIGINS}")
    if attribution.category is None:
        raise ValueError(
            "cannot reflow a failure with no category; attribute the first error first"
        )
    kind, constraints = CONSTRUCTION[attribution.category]
    return RegressionTask(
        task_id=task_id,
        kind=kind,
        origin=origin,
        from_category=attribution.category,
        constraints=constraints,
    )


@dataclass(frozen=True, slots=True)
class Holdout:
    """The retained tasks that measure generalisation rather than recall."""

    task_ids: frozenset[str]

    def verify_disjoint(self, learned: Iterable[str]) -> None:
        """Refuse overlap between learned material and the retained set (V05)."""
        overlap = sorted(self.task_ids.intersection(learned))
        if overlap:
            raise LearnedMaterialInHoldout(
                f"learned material appears in the holdout: {', '.join(overlap)}"
            )

    def retained_among(self, candidates: Iterable[str]) -> tuple[str, ...]:
        """The subset of candidates that is still held out and therefore worth running."""
        return tuple(sorted(self.task_ids.intersection(candidates)))
