"""Show source-owned textual status in the unified terminal."""

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import ExtensionAPI, ExtensionContext


def setup(api: ExtensionAPI) -> None:
    """Keep a completed-turn counter in this extension's status line."""
    completed = 0

    def started(event: object, context: ExtensionContext) -> None:
        del event
        context.ui.set_status("turns", f"Completed: {completed}")

    def settled(event: object, context: ExtensionContext) -> None:
        nonlocal completed
        if isinstance(event, AgentSettledEvent) and event.status == "succeeded":
            completed += 1
        context.ui.set_status("turns", f"Completed: {completed}")

    api.on("session_start", started)
    api.on("agent_settled", settled)
