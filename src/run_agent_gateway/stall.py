"""Stall detection: notice a turn that has stopped making progress and say so once.

Progress is observed, not inferred: the runner stamps the session every time the agent
emits an event. The watcher compares that stamp with the stall budget for chats whose turn
is still running and sends one notice per stall, then clears it when progress resumes or the
turn ends. It never kills anything; the user decides with ``/stop``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnActivity:
    started_at: float
    last_progress_at: float
    last_label: str = ""
    notified: bool = False
    events: int = 0


@dataclass(slots=True)
class StallReport:
    session_key: str
    idle_seconds: float
    last_label: str


@dataclass(slots=True)
class StallMonitor:
    timeout_seconds: float
    clock: Callable[[], float] = time.monotonic
    _turns: dict[str, TurnActivity] = field(default_factory=dict)

    def begin(self, session_key: str) -> None:
        now = self.clock()
        self._turns[session_key] = TurnActivity(now, now)

    def progress(self, session_key: str, label: str = "") -> None:
        turn = self._turns.get(session_key)
        if turn is None:
            return
        turn.last_progress_at = self.clock()
        turn.events += 1
        if label:
            turn.last_label = label
        turn.notified = False

    def end(self, session_key: str) -> TurnActivity | None:
        return self._turns.pop(session_key, None)

    def activity(self, session_key: str) -> TurnActivity | None:
        return self._turns.get(session_key)

    def stalled(self) -> list[StallReport]:
        """Turns past the budget that have not been reported since their last progress."""
        now = self.clock()
        reports: list[StallReport] = []
        for key, turn in self._turns.items():
            idle = now - turn.last_progress_at
            if idle >= self.timeout_seconds and not turn.notified:
                turn.notified = True
                reports.append(StallReport(key, idle, turn.last_label))
        return reports


def format_stall_notice(report: StallReport) -> str:
    minutes = report.idle_seconds / 60
    tail = f"，最近一步：{report.last_label}" if report.last_label else ""
    return f"当前任务已有 {minutes:.0f} 分钟没有新进展{tail}。仍在等待；如需中断请发送 /stop。"


__all__ = ["StallMonitor", "StallReport", "TurnActivity", "format_stall_notice"]
