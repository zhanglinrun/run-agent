"""Failure attribution: locate the first error, not the last (T-010, T-011).

Chapter 7's reason for caring is that an end-to-end verdict says only pass or fail:
"the failing task scores zero, and that zero does not say whether the agent erred in
choosing a route or skipped a top-up step, let alone what to change next." For a
system that is meant to keep improving, that missing information is the whole point.

So attribution targets "the first error in the trajectory that caused the task to
deviate", because later errors are usually knock-on effects and treating the last
raised error as the root cause sends the fix to the wrong place. Each attribution
carries reviewable evidence, a step and what it did, since the output is meant to
become a regression task rather than a score.

Chapter 7 notes the analysis can be assisted by a model but not delegated to one.
Classification is therefore an input here: this module decides which classified fault
is first, which is deterministic, and leaves the judgement of what a fault is to the
caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

CATEGORIES = (
    "流程缺失",  # a missing process step
    "工具调用错误",  # tool-call error
    "执行异常",  # execution exception
    "完成度与逻辑错误",  # completeness and logic error
    "需求理解与歧义",  # requirement misunderstanding or ambiguity
    "症状修复与验证造假",  # symptom patching and verification fraud
    "信息反馈",  # feedback
)

CATEGORY_SET = frozenset(CATEGORIES)


@dataclass(frozen=True, slots=True)
class Step:
    """One observable step of a trajectory, with any fault already classified."""

    index: int
    kind: str
    detail: str
    failed: bool = False
    category: str | None = None

    def __post_init__(self) -> None:
        if self.category is not None and self.category not in CATEGORY_SET:
            raise ValueError(f"unknown category: {self.category!r}; expected one of {CATEGORIES}")

    @property
    def faulty(self) -> bool:
        return self.failed or self.category is not None

    def describe(self) -> str:
        return f"step {self.index} {self.kind}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Attribution:
    """Which step caused the deviation, under which category, and what followed."""

    first_error_index: int | None = None
    category: str | None = None
    evidence: tuple[str, ...] = ()
    subsequent_errors: tuple[int, ...] = field(default=())


def attribute(steps: Iterable[Step]) -> Attribution:
    """Return the first fault as root cause, with later faults as knock-on effects."""
    ordered: Sequence[Step] = tuple(steps)
    for step in ordered:
        if step.category is not None and step.category not in CATEGORY_SET:
            raise ValueError(f"unknown category: {step.category!r}")
    faults = [step for step in ordered if step.faulty]
    if not faults:
        return Attribution()
    root = faults[0]
    return Attribution(
        first_error_index=root.index,
        category=root.category,
        evidence=(root.describe(),),
        subsequent_errors=tuple(step.index for step in faults[1:]),
    )
