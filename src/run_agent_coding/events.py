"""Pi-compatible coding-session events consumed by frontends and SDK users."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from run_agent_core.events import AgentEvent
from run_agent_core.messages import AgentMessage, WireModel
from run_agent_core.session.contracts import RunStatus
from run_agent_core.session.entries import SessionEntry


class SessionAgentEndEvent(WireModel):
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage] = Field(default_factory=list)
    will_retry: bool = Field(False)


class AgentSettledEvent(WireModel):
    type: Literal["agent_settled"] = "agent_settled"
    run_id: str
    session_id: str
    branch_id: str
    status: RunStatus
    head_id: str | None
    watermark: int
    snapshot_id: str | None = None


class QueueUpdateEvent(WireModel):
    type: Literal["queue_update"] = "queue_update"
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = Field(())


CompactionReason = Literal["manual", "threshold", "overflow"]


class EntryAppendedEvent(WireModel):
    type: Literal["entry_appended"] = "entry_appended"
    entry: SessionEntry


class SessionInfoChangedEvent(WireModel):
    type: Literal["session_info_changed"] = "session_info_changed"
    name: str | None = None


class ThinkingLevelSelectEvent(WireModel):
    type: Literal["thinking_level_select"] = "thinking_level_select"
    level: str
    previous_level: str | None = None


type SessionOwnEvent = Annotated[
    SessionAgentEndEvent
    | AgentSettledEvent
    | QueueUpdateEvent
    | EntryAppendedEvent
    | SessionInfoChangedEvent
    | ThinkingLevelSelectEvent,
    Field(discriminator="type"),
]
type CodingSessionEvent = AgentEvent | SessionOwnEvent
type AgentSessionEvent = CodingSessionEvent
