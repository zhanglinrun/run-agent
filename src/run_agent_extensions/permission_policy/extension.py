"""Permission policy aligned with my-pi-agent ``PermissionGate``."""

from __future__ import annotations

from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    ToolCallHookEvent,
    ToolCallHookResult,
)

from .permissions import PermissionGate, PermissionMode, PermissionRequest

_MODES = frozenset({"review", "autonomous", "yolo", "strict"})


def setup(api: ExtensionAPI) -> None:
    """Register ``PermissionGate`` on ``tool_call``; default mode is ``review``."""
    raw = api.context.environment.get("RUN_AGENT_PERMISSION_MODE", "review").strip().casefold()
    mode = cast(PermissionMode, raw) if raw in _MODES else "review"

    async def guard(
        event: ToolCallHookEvent,
        context: ExtensionContext,
    ) -> ToolCallHookResult:
        async def confirm(req: PermissionRequest) -> bool:
            lines = [f"target: {req.target}"]
            if req.preview:
                lines.append(str(req.preview))
            return await context.ui.confirm(f"Approve {req.action}", "\n".join(lines))

        gate = PermissionGate(
            mode=mode,
            confirm_callback=confirm if context.has_ui else None,
        )
        return await gate(event)

    api.on("tool_call", cast(ExtensionHandler, guard))
