"""Slash completion using live session resources, inspired by Tau's TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from run_agent_coding.session import CodingSession


@dataclass(frozen=True, slots=True)
class CompletionItem:
    text: str
    description: str


UI_COMMANDS = {
    "/theme": "Choose the terminal theme",
    "/sidebar": "Toggle session details",
    "/clear": "Clear the visible transcript; keep saved history",
    "/quit": "Exit the current session",
    "/stop": "Cancel the running task",
    "/queue": "Queue a follow-up for the running task",
    "/expand": "Expand tool results",
}


def get_completions(session: CodingSession, text: str) -> list[CompletionItem]:
    """Complete only a leading slash token; never replace command arguments."""
    if not text.startswith("/") or text.startswith("//") or any(c.isspace() for c in text):
        return []
    options = dict(UI_COMMANDS)
    for command in session.command_registry.list_commands():
        for name in (command.name, *command.aliases):
            options[f"/{name}"] = command.description
    options.update(
        {f"/skill:{skill.name}": skill.description or "Use this skill" for skill in session.skills}
    )
    for template in session.prompt_templates:
        options.setdefault(f"/{template.name}", template.description or "Prompt template")
    return [
        CompletionItem(name, description)
        for name, description in sorted(options.items())
        if name.casefold().startswith(text.casefold())
    ]
