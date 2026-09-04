"""Optional read-only planning policy implemented as a regular extension."""

from __future__ import annotations

from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
    ToolCallHookEvent,
    ToolCallHookResult,
)

_MUTATING_TOOLS = frozenset({"write", "edit", "bash", "memory", "verify"})


def setup(api: ExtensionAPI) -> None:
    """Register a session-local read-only planning mode."""
    state = {"enabled": False}

    def command(args: str, context: ExtensionCommandContext) -> str:
        del context
        requested = args.strip().casefold()
        if requested in {"", "status"}:
            return f"Plan mode is {'on' if state['enabled'] else 'off'}."
        if requested in {"on", "enable", "start"}:
            state["enabled"] = True
            return "Plan mode enabled. Mutating tools are blocked."
        if requested in {"off", "disable", "stop"}:
            state["enabled"] = False
            return "Plan mode disabled."
        return "Usage: /plan [on|off|status]"

    def guard(event: ToolCallHookEvent, context: ExtensionContext) -> ToolCallHookResult:
        del context
        if not state["enabled"] or event.tool_name not in _MUTATING_TOOLS:
            return ToolCallHookResult()
        if event.tool_name == "memory" and event.arguments.get("action") in {"search", "list"}:
            return ToolCallHookResult()
        return ToolCallHookResult(
            block=True,
            reason="plan mode permits inspection only; run /plan off before modifying state",
        )

    api.register_command(
        "plan",
        command,
        description="Enable or disable read-only planning mode.",
        usage="/plan [on|off|status]",
    )
    api.on("tool_call", cast(ExtensionHandler, guard))
    api.add_prompt_guideline(
        "When plan mode is enabled, inspect and reason only; do not attempt mutating tools."
    )
