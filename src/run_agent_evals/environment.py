"""The five elements chapter 7 requires of a repeatable evaluation environment.

Chapter 7 names them: dataset, resettable state, atomic tools, scoring criteria, and
an interaction protocol - "if any one of the five is missing, the evaluation cannot
form a repeatable loop".

It also separates human-interaction environments from tool-calling ones. A coding
agent is the second kind, so this module does not copy the simulated user, the
patience exhaustion or the ``###STOP###`` signal, which belong to the first kind.
What a coding run needs instead is the conditions under which it stops.

Tool atomicity is enforced rather than documented because chapter 7 gives the reason:
abstracting too high turns the evaluation into a test of a single function call, since
the tool itself absorbs the planning and reasoning the evaluation exists to measure.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from run_agent_evals.task_spec import TaskSpec, materialize_environment

ATOMIC_TOOLS = frozenset({"read", "write", "edit", "bash", "glob", "grep", "ls"})


class NonAtomicTool(ValueError):
    """Raised for a tool that does the planning the evaluation is meant to measure."""


def require_atomic_tools(tools: Iterable[str]) -> None:
    """Refuse a tool set containing anything above an atomic operation."""
    coarse = sorted(set(tools) - ATOMIC_TOOLS)
    if coarse:
        raise NonAtomicTool(
            f"tools must be atomic operations; these absorb the planning being "
            f"measured: {', '.join(coarse)}"
        )


@dataclass(frozen=True, slots=True)
class TerminationPolicy:
    """When a run stops, and why.

    Chapter 7 requires the protocol to state its termination conditions. For a
    tool-calling agent these are completion, cancellation, budget and turn limit; the
    simulated user's patience belongs to the human-interaction kind.
    """

    max_turns: int = 40
    budget_seconds: float = 120.0

    def stop_reason(
        self,
        *,
        turns: int,
        elapsed: float,
        cancelled: bool = False,
        finished: bool = False,
    ) -> str | None:
        """Return why the run must stop now, or ``None`` to keep going."""
        if finished:
            return "completed"
        if cancelled:
            return "cancelled"
        if elapsed >= self.budget_seconds:
            return "budget exhausted"
        if turns >= self.max_turns:
            return "turn limit"
        return None


@dataclass(slots=True)
class EvaluationEnvironment:
    """One task's evaluation environment: dataset, state, and protocol.

    The state is the workspace, and it is resettable by construction: resets are
    rebuilds from the pristine environment, so a run cannot inherit residue from the
    previous one. ``digest`` makes that claim checkable instead of asserted.
    """

    dataset: TaskSpec
    workspace: Path
    protocol: TerminationPolicy = field(default_factory=TerminationPolicy)

    def reset(self) -> None:
        """Return the workspace to the task's initial state."""
        materialize_environment(self.dataset, self.workspace)

    def digest(self) -> str:
        """Content hash of the current state, so resets can be compared."""
        accumulator = hashlib.sha256()
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            accumulator.update(path.relative_to(self.workspace).as_posix().encode())
            accumulator.update(b"\0")
            accumulator.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
            accumulator.update(b"\n")
        return accumulator.hexdigest()
