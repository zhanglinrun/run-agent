"""hermes-agent's two review nudges, as counters the extension keeps per session.

hermes decides *when* to fork a background review with two counters on the agent:

- ``_turns_since_memory`` goes up by one on every user turn and back to zero whenever
  the ``memory`` tool ran. When it reaches ``memory.nudge_interval`` (10) at the start of
  a turn, that turn ends with a memory review.
- ``_iters_since_skill`` goes up by one on every model iteration (each API call of the
  tool loop) while ``skill_manage`` is available, and back to zero whenever that tool
  ran. When it reaches ``skills.creation_nudge_interval`` (10) at the end of a turn,
  that turn ends with a skill review.

Both fire together for a combined review. On a resumed session the memory counter is
hydrated from the persisted history (``prior_user_turns % interval``, hermes #22357) so
a long conversation does not restart the cadence from zero after every restart.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from run_agent_core.messages import AgentMessage, UserMessage

MEMORY_TOOL = "memory"
SKILL_TOOL = "skill_manage"


@dataclass(frozen=True, slots=True)
class NudgeFlags:
    """Which reviews a finished turn asked for."""

    review_memory: bool = False
    review_skills: bool = False

    @property
    def any(self) -> bool:
        return self.review_memory or self.review_skills


@dataclass(slots=True)
class NudgeCounters:
    memory_interval: int = 10
    skill_interval: int = 10
    turns_since_memory: int = 0
    iters_since_skill: int = 0
    user_turn_count: int = 0
    skill_tool_available: bool = True
    _memory_due: bool = False

    # -- hydration -------------------------------------------------------------------

    def hydrate(self, history: Iterable[AgentMessage]) -> None:
        """Seed the memory cadence from a resumed transcript (hermes #22357).

        Only when nothing has been counted yet in this process: a reload mid-session
        must not reset a counter that already moved.
        """
        if self.user_turn_count:
            return
        prior = sum(1 for message in history if isinstance(message, UserMessage))
        if prior <= 0:
            return
        self.user_turn_count = prior
        if self.memory_interval > 0 and self.turns_since_memory == 0:
            self.turns_since_memory = prior % self.memory_interval

    # -- counting --------------------------------------------------------------------

    def on_user_turn(self) -> bool:
        """Count one user turn; True when this turn should end with a memory review.

        The flag is latched until :meth:`take_flags` reads it, the way hermes sets
        ``should_review_memory`` at turn start and consumes it at turn end.
        """
        self.user_turn_count += 1
        if self.memory_interval <= 0:
            return False
        self.turns_since_memory += 1
        if self.turns_since_memory >= self.memory_interval:
            self._memory_due = True
            self.turns_since_memory = 0
            return True
        return False

    def on_iteration(self) -> None:
        """Count one model iteration of the tool loop."""
        if self.skill_interval > 0 and self.skill_tool_available:
            self.iters_since_skill += 1

    def on_tool_ran(self, tool_name: str) -> None:
        """A memory or skill write resets its counter; the model is already saving."""
        if tool_name == MEMORY_TOOL:
            self.turns_since_memory = 0
        elif tool_name == SKILL_TOOL:
            self.iters_since_skill = 0

    def take_flags(self) -> NudgeFlags:
        """Read and clear what the finishing turn asked for."""
        review_memory = self._memory_due
        self._memory_due = False
        review_skills = (
            self.skill_interval > 0
            and self.skill_tool_available
            and self.iters_since_skill >= self.skill_interval
        )
        if review_skills:
            self.iters_since_skill = 0
        return NudgeFlags(review_memory=review_memory, review_skills=review_skills)

    def peek(self) -> NudgeFlags:
        """What :meth:`take_flags` would return, without consuming it."""
        return NudgeFlags(
            review_memory=self._memory_due,
            review_skills=(
                self.skill_interval > 0
                and self.skill_tool_available
                and self.iters_since_skill >= self.skill_interval
            ),
        )


__all__ = ["MEMORY_TOOL", "SKILL_TOOL", "NudgeCounters", "NudgeFlags"]
