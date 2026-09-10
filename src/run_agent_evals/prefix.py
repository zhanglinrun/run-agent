"""Trajectory-prefix regression tasks (T-012, T-013).

Chapter 7 separates two kinds of regression task. An end-to-end task runs the whole
workflow and checks the final state, which is closest to production but cannot say
where it failed. A trajectory-prefix task freezes the context, dialogue, tool returns
and environment state up to the first error and asks only for the next observable
action - cheaper, and able to isolate one policy or tool decision. For an agent that
has to be reliable the chapter rates the second as the more important of the two.

The answer is a set of acceptable actions rather than a single action, because
several next moves can be correct: read the repository rules first, ask the user
first, refuse the dangerous operation. Listing forbidden actions alongside makes the
unsafe moves explicit instead of implied.

Judgement is deterministic - membership in two sets - which is deliberate. Chapter 7
shows the scoring for this kind of task done by rules, and reserving model judgement
for dimensions that cannot be asserted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


class UndefinedAcceptableSet(ValueError):
    """Raised when a prefix task cannot say what would be acceptable."""


@dataclass(frozen=True, slots=True)
class FrozenPrefix:
    """Everything up to the decision boundary: context, tool returns, state, request."""

    context: tuple[str, ...]
    tool_returns: tuple[str, ...]
    request: str
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PrefixTask:
    """A prefix plus the sets that decide whether the next action is acceptable."""

    prefix: FrozenPrefix
    acceptable: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.acceptable:
            raise UndefinedAcceptableSet(
                "a prefix task must define its acceptable actions; a single expected "
                "answer would reject correct alternatives"
            )


@dataclass(frozen=True, slots=True)
class PrefixVerdict:
    """Whether one proposed next action is acceptable, and why."""

    action: str
    accepted: bool
    reason: str


def judge(task: PrefixTask, action: str) -> PrefixVerdict:
    """Decide one next action against the task's acceptable and forbidden sets.

    Forbidden wins over acceptable: a dangerous action that someone also listed as
    acceptable must still be refused, and the outcome should say which rule decided.
    """
    if action in task.forbidden:
        return PrefixVerdict(action, False, "forbidden action")
    if action in task.acceptable:
        return PrefixVerdict(action, True, "acceptable action")
    return PrefixVerdict(action, False, "not in the acceptable set")


def verdicts(task: PrefixTask, actions: Iterable[str]) -> tuple[PrefixVerdict, ...]:
    """Judge several proposed actions, in the order given."""
    return tuple(judge(task, action) for action in actions)
