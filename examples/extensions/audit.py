"""Minimal trusted Run Agent extension example."""

from __future__ import annotations

from typing import Any

from agents.extensions import EXTENSION_API_VERSION, ExtensionContext


EXTENSION_NAME = "example-audit"
EXTENSION_REQUIRES = ("workspace-tools",)


def setup(api: Any) -> None:
    if api.api_version != EXTENSION_API_VERSION:
        raise RuntimeError("unsupported Run Agent extension API")

    async def audit_command(args: str, context: ExtensionContext) -> str:
        suffix = f" args={args}" if args else ""
        return (
            f"session={context.state.session_id} "
            f"lane={context.state.lane_id} "
            f"workspace={context.workspace}{suffix}"
        )

    def prompt(_turn: Any, _context: ExtensionContext) -> str:
        return "Extension example-audit is active."

    api.register_command(
        "audit",
        audit_command,
        description="Show the current task audit identity",
    )
    api.contribute_prompt("example-audit", prompt, priority=200)
