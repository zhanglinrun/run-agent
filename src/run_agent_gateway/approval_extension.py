"""Session extension that holds a dangerous ``bash`` call until the chat approves it.

Loaded by the gateway runner into every agent it opens (via ``extension_paths``). The
extension registers a ``tool_call`` hook; for a ``bash`` call whose command matches a
dangerous pattern it asks the runner, through the contextvar-registered gate, to prompt
the chat and waits for the answer. Outside a gateway process the gate is unset and the
hook does nothing, so the same extension is inert elsewhere.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    ToolCallHookEvent,
    ToolCallHookResult,
)
from run_agent_gateway.approval import DangerVerdict, detect_dangerous_command

# (command, verdict) -> reason to block, or None when the command may run.
ApprovalGate = Callable[[str, DangerVerdict], Awaitable[str | None]]

_gate: contextvars.ContextVar[ApprovalGate | None] = contextvars.ContextVar(
    "gateway_approval_gate", default=None
)
_session_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gateway_approval_session_key", default=None
)


def set_approval_gate(gate: ApprovalGate | None) -> contextvars.Token[ApprovalGate | None]:
    return _gate.set(gate)


def reset_approval_gate(token: contextvars.Token[ApprovalGate | None]) -> None:
    _gate.reset(token)


def set_approval_session_key(key: str | None) -> contextvars.Token[str | None]:
    return _session_key.set(key)


def reset_approval_session_key(token: contextvars.Token[str | None]) -> None:
    _session_key.reset(token)


def current_approval_session_key() -> str | None:
    return _session_key.get()


def setup(api: ExtensionAPI) -> None:
    async def guard(
        event: ToolCallHookEvent, context: ExtensionContext
    ) -> ToolCallHookResult | None:
        if event.tool_name != "bash":
            return None
        gate = _gate.get()
        if gate is None:
            return None
        command = event.arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        verdict = detect_dangerous_command(command)
        if not verdict.dangerous:
            return None
        reason = await gate(command, verdict)
        if reason is None:
            return None
        return ToolCallHookResult(block=True, reason=reason)

    api.on("tool_call", cast(ExtensionHandler, guard))


__all__ = [
    "ApprovalGate",
    "current_approval_session_key",
    "reset_approval_gate",
    "reset_approval_session_key",
    "set_approval_gate",
    "set_approval_session_key",
    "setup",
]
