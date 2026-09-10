"""Reusable runtime-simulation support for deterministic harness tests.

Three pieces, all consumed by the later Runtime/learning suites and by budget
and fault tests: named fault points, controllable tools, and event-conditioned
multi-turn input. Nothing here uses a timer to create a race: turn scripts fire
on events, and a tool delay is explicit and opt-in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

SleepFunction = Callable[[float], Awaitable[None]]


class InjectedFault(RuntimeError):
    """Raised at a fault point a caller deliberately configured to fail."""


class FaultJournal:
    """Records named boundaries and raises at the ones marked to fail."""

    def __init__(self) -> None:
        self._points: list[str] = []
        self._failures: dict[str, str] = {}
        self._failed: list[str] = []

    def fail_at(self, point: str, error: str | None = None) -> None:
        """Make every later hit of this point raise."""
        self._failures[point] = error or f"injected fault at {point}"

    def hit(self, point: str) -> None:
        """Record a boundary crossing, raising when this point is configured to fail."""
        self._points.append(point)
        message = self._failures.get(point)
        if message is None:
            return
        self._failed.append(point)
        raise InjectedFault(message)

    def points(self) -> tuple[str, ...]:
        """Every boundary crossed, in order, including the failing ones."""
        return tuple(self._points)

    def failed_at(self) -> tuple[str, ...]:
        """The boundaries that actually raised."""
        return tuple(self._failed)


@dataclass(frozen=True)
class ToolInvocation:
    """One completed tool call, as observed by the caller."""

    arguments: Mapping[str, Any]


class ControlledTool:
    """A tool double with an invocation counter, an explicit delay and failure injection.

    ``sleep`` is injectable so a test can observe the requested delay without
    asserting wall-clock time: real timers on Windows fire early (a requested
    0.05s was measured returning at 0.047s), which makes lower-bound timing
    assertions flaky.
    """

    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        sleep: SleepFunction | None = None,
    ) -> None:
        self.name = name
        self.delay = delay
        self.calls = 0
        self._sleep: SleepFunction = sleep or asyncio.sleep
        self._pending: list[str] = []

    def fail_next(self, error: str) -> None:
        """Queue one failure for the next invocation."""
        self._pending.append(error)

    async def invoke(self, arguments: Mapping[str, Any]) -> ToolInvocation:
        """Count the call, wait the configured delay, then fail or succeed."""
        self.calls += 1
        if self.delay:
            await self._sleep(self.delay)
        if self._pending:
            raise InjectedFault(self._pending.pop(0))
        return ToolInvocation(arguments=dict(arguments))


class TurnScript:
    """Event-conditioned multi-turn input; each event fires at most once."""

    def __init__(self) -> None:
        self._steps: dict[str, str] = {}
        self._fired: list[str] = []

    def when(self, event: str, text: str) -> None:
        """Script ``text`` to be sent the first time ``event`` occurs."""
        if event in self._steps:
            raise ValueError(f"{event} already has a scripted turn")
        self._steps[event] = text

    def on_event(self, event: str) -> str | None:
        """Return the text to send for this event, or None when nothing is due."""
        if event not in self._steps or event in self._fired:
            return None
        self._fired.append(event)
        return self._steps[event]

    def pending(self) -> int:
        """How many scripted turns have not fired yet."""
        return len(self._steps) - len(self._fired)

    def unfired(self) -> tuple[str, ...]:
        """Scripted events that never occurred, in declaration order."""
        return tuple(event for event in self._steps if event not in self._fired)
