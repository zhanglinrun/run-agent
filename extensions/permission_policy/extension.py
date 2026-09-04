"""Optional workspace and destructive-command policy extension."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    ToolCallHookEvent,
    ToolCallHookResult,
)

_MUTATING_TOOLS = frozenset({"write", "edit", "bash", "memory", "verify"})
_DESTRUCTIVE_SHELL = re.compile(
    r"(?:^|[;&|]\s*)(?:rm\s+-[^\n]*r[^\n]*f\s+[/~]|git\s+(?:reset\s+--hard|clean\s+-[^\n]*f)|"
    r"Remove-Item\b[^\n]*(?:-Recurse|-Force)|format(?:\.com)?\s+[A-Za-z]:)",
    re.IGNORECASE,
)


def setup(api: ExtensionAPI) -> None:
    """Register an opt-in permission policy around mutating tools."""
    mode = api.context.environment.get("RUN_AGENT_PERMISSION_MODE", "guarded").strip().casefold()
    if mode not in {"allow", "guarded", "ask", "deny"}:
        mode = "guarded"

    async def guard(
        event: ToolCallHookEvent,
        extension_context: ExtensionContext,
    ) -> ToolCallHookResult:
        if event.tool_name not in _MUTATING_TOOLS:
            return ToolCallHookResult()
        if mode == "allow":
            return ToolCallHookResult()
        reason = _denial_reason(
            event,
            cwd=extension_context.cwd,
            deny_all=mode == "deny",
        )
        if reason is not None:
            return ToolCallHookResult(block=True, reason=reason)
        if mode != "ask":
            return ToolCallHookResult()
        if not extension_context.has_ui:
            return ToolCallHookResult(
                block=True,
                reason="permission mode is ask, but no interactive UI is attached",
            )
        approved = await extension_context.ui.confirm(
            "Approve tool call",
            f"Allow {event.tool_name} to modify local state?",
        )
        return ToolCallHookResult(
            block=not approved,
            reason=None if approved else "user denied the tool call",
        )

    api.on("tool_call", cast(ExtensionHandler, guard))


def _denial_reason(
    event: ToolCallHookEvent,
    *,
    cwd: Path,
    deny_all: bool,
) -> str | None:
    if deny_all:
        return "permission policy denies mutating tools"
    if event.tool_name in {"write", "edit"}:
        raw_path = event.arguments.get("path")
        if isinstance(raw_path, str):
            candidate = Path(raw_path)
            resolved = (
                (cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
            )
            try:
                resolved.relative_to(cwd.resolve())
            except ValueError:
                return f"write target is outside the workspace: {resolved}"
    if event.tool_name in {"bash", "verify"}:
        command = event.arguments.get("command")
        if isinstance(command, str) and _DESTRUCTIVE_SHELL.search(command):
            return "destructive shell command requires an explicit allow policy"
    return None
