"""A host can durably accept input at the agent's next steering boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from run_agent_core.session.contracts import AppendReceipt, RunToken
from run_agent_core.session.entries import SessionEntry


@dataclass(frozen=True, slots=True)
class InputBoundary:
    token: RunToken
    branch_id: str
    expected_head: str | None
    pending_entries: tuple[SessionEntry, ...]


@dataclass(frozen=True, slots=True)
class CommittedInput:
    """Entries include the supplied pending prefix followed by new input messages.

    The host commits every entry and its input receipt in one transaction. Returning
    None means no input was available and the pending prefix was not committed.
    """

    entries: tuple[SessionEntry, ...]
    receipt: AppendReceipt


InputSource = Callable[[InputBoundary], Awaitable[CommittedInput | None]]
