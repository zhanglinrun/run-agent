"""Pi-compatible JSON event stream renderer."""

import typer

from run_agent_coding.events import AutoRetryEndEvent, CodingSessionEvent
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage


class JsonEventRenderer:
    def __init__(self) -> None:
        self._failed = False

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, AutoRetryEndEvent) and event.success:
            self._failed = False
        if (
            isinstance(event, MessageEndEvent)
            and isinstance(event.message, AssistantMessage)
            and event.message.stop_reason == "error"
        ):
            self._failed = True
        typer.echo(event.model_dump_json(by_alias=True, exclude_none=True))

    def finish(self) -> bool:
        return not self._failed
