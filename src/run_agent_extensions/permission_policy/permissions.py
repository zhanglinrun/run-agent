"""PermissionGate aligned with my-pi-agent: ToolCallHook-style review / yolo / strict."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from run_agent_coding.extensions import ToolCallHookEvent, ToolCallHookResult
from run_agent_core.types import JSONValue

PermissionMode = Literal["review", "autonomous", "yolo", "strict"]
READONLY_TOOLS = frozenset({"read", "grep", "find"})
SAFE_BASH_PREFIXES = (
    "git status",
    "git diff",
    "git log",
    "pytest",
    "python -m pytest",
    "uv run",
)


@dataclass(frozen=True)
class PermissionRequest:
    """Permission prompt payload (my-pi-agent ``PermissionRequest``)."""

    action: str
    target: str
    details: dict[str, Any] = field(default_factory=dict)
    preview: Any = None


class PermissionGate:
    """Non-invasive approval gate invoked as a ``tool_call`` hook."""

    def __init__(
        self,
        mode: PermissionMode = "review",
        confirm_callback: Callable[[PermissionRequest], Awaitable[bool] | bool] | None = None,
    ) -> None:
        self.mode: PermissionMode = "autonomous" if mode == "yolo" else mode
        self.confirm_callback = confirm_callback

    async def __call__(self, hook: ToolCallHookEvent) -> ToolCallHookResult:
        if self.mode == "autonomous":
            return ToolCallHookResult()

        tool_name = hook.tool_name
        args: Mapping[str, JSONValue] = hook.arguments or {}

        if self.mode != "strict" and tool_name in READONLY_TOOLS:
            return ToolCallHookResult()

        if tool_name == "bash" and self.mode == "review":
            cmd = str(args.get("command", "")).strip()
            if any(cmd.startswith(p) for p in SAFE_BASH_PREFIXES):
                return ToolCallHookResult()

        if not self.confirm_callback:
            return ToolCallHookResult()

        target = str(args.get("path") or args.get("command") or tool_name)
        preview = None
        if tool_name in ("write", "edit"):
            preview = args.get("content") or str(args.get("edits", ""))

        req = PermissionRequest(
            action=tool_name,
            target=target,
            details=dict(args),
            preview=preview,
        )

        res = self.confirm_callback(req)
        approved = await res if inspect.isawaitable(res) else res
        if not approved:
            return ToolCallHookResult(
                block=True,
                reason=f"用户拒绝了工具 [{tool_name}] 的执行请求。",
            )
        return ToolCallHookResult()
