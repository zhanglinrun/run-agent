"""Permission, Plan Mode, and MCP extensions."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
from pathlib import Path
from typing import Any

from ..policy import PolicyEngine, WorkspaceBoundary
from ..policy.shell import ShellRisk, classify_shell_command
from ..runtime.contracts import EventType
from ..runtime.hooks import ToolCallDecision
from ..session import OperationType
from ..tools.mcp import McpManager
from .contracts import ExtensionAPI, ExtensionContext, ExtensionEvent


@dataclass
class PermissionState:
    mode: str = "default"
    plan_file: Path | None = None
    mode_before_plan: str = "default"
    read_only: bool = False
    pre_plan_tools: frozenset[str] | None = None
    pre_plan_ceiling: frozenset[str] | None = None


READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search", "compact_context", "skill", "tool_search"}


def _parse_rule(rule: str) -> tuple[str, str | None]:
    text = str(rule).strip()
    if "(" in text and text.endswith(")"):
        name, pattern = text[:-1].split("(", 1)
        return name.strip(), pattern.strip()
    return text, None


def _matches_rule(rule: tuple[str, str | None], tool_name: str, value: dict[str, Any]) -> bool:
    name, pattern = rule
    if name != tool_name:
        return False
    if pattern is None:
        return True
    candidate = str(value.get("command") or value.get("file_path") or "")
    return candidate.startswith(pattern[:-1]) if pattern.endswith("*") else candidate == pattern


def _permission_rule(workspace: Path, tool_name: str, value: dict[str, Any]) -> str | None:
    allow: list[tuple[str, str | None]] = []
    deny: list[tuple[str, str | None]] = []
    for path in (Path.home() / ".run" / "settings.json", workspace / ".run" / "settings.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        permissions = raw.get("permissions") if isinstance(raw, dict) else None
        if not isinstance(permissions, dict):
            continue
        allow.extend(_parse_rule(item) for item in permissions.get("allow", ()))
        deny.extend(_parse_rule(item) for item in permissions.get("deny", ()))
    normalized = PolicyEngine.normalize_name(tool_name)
    if any(_matches_rule(rule, normalized, value) for rule in deny):
        return "deny"
    if any(_matches_rule(rule, normalized, value) for rule in allow):
        return "allow"
    return None


async def _observe_permission(
    context: ExtensionContext,
    call_id: str,
    name: str,
    decision: dict[str, Any],
) -> None:
    payload = {"call_id": call_id, "name": name, **decision}
    context.repository.append_operation(
        context.state.session_id,
        context.state.lane_id,
        OperationType.PERMISSION_DECIDED,
        payload,
        run_id=context.state.run_id,
    )
    context.trace.emit(EventType.PERMISSION_DECISION, **payload)


async def _authorize(
    name: str,
    value: dict[str, Any],
    context: ExtensionContext,
    call_id: str,
) -> ToolCallDecision:
    state = context.require("permission_state")
    if state.read_only:
        normalized = PolicyEngine.normalize_name(name)
        if normalized == "run_shell" and state.mode != "plan":
            risk = classify_shell_command(str(value.get("command") or "")).risk
            if risk not in {ShellRisk.READ_ONLY, ShellRisk.VERIFY}:
                decision = {
                    "action": "deny",
                    "message": "Delegated read-only roles may run only inspection or verification commands.",
                    "reason_code": "delegated_read_only",
                    "final": True,
                    "source": "policy",
                }
                await _observe_permission(context, call_id, name, decision)
                return ToolCallDecision("deny", decision["message"])
        elif normalized not in READ_ONLY_TOOLS:
            decision = {
                "action": "deny",
                "message": f"Blocked for delegated read-only role: {normalized}",
                "reason_code": "delegated_read_only",
                "final": True,
                "source": "policy",
            }
            await _observe_permission(context, call_id, name, decision)
            return ToolCallDecision("deny", decision["message"])

    engine = PolicyEngine(WorkspaceBoundary(context.workspace))
    decision = engine.decide(
        name,
        value,
        mode=state.mode,
        plan_file_path=str(state.plan_file) if state.plan_file else None,
        rule_result=_permission_rule(context.workspace, name, value),
    ).to_dict()
    action = str(decision.get("action") or "deny")
    await _observe_permission(
        context,
        call_id,
        name,
        {**decision, "source": "policy", "final": action != "confirm"},
    )
    if action == "confirm":
        callback = context.task.runtime.permissions.confirm
        allowed = False
        if callback is not None:
            result = callback(str(decision.get("message") or name))
            allowed = bool(await result) if inspect.isawaitable(result) else bool(result)
        final = {
            **decision,
            "action": "allow" if allowed else "deny",
            "source": "user_confirmation",
            "final": True,
        }
        await _observe_permission(context, call_id, name, final)
        if not allowed:
            return ToolCallDecision("deny", "User denied this action.")
        return ToolCallDecision()
    if action != "allow":
        return ToolCallDecision("deny", str(decision.get("message") or "Tool call denied."))
    return ToolCallDecision()


def setup_permissions(api: ExtensionAPI) -> None:
    async def session_start(_event: ExtensionEvent, context: ExtensionContext) -> None:
        mode = context.task.runtime.permissions.mode
        context.services["permission_state"] = PermissionState(
            mode=mode,
            mode_before_plan="acceptEdits" if mode == "plan" else mode,
        )

    api.provide("authorizer", _authorize)
    api.on("session_start", session_start)


def _plan_path(context: ExtensionContext) -> Path:
    configured = context.task.runtime.permissions.plan_file
    path = configured or context.workspace / ".run" / "plans" / f"{context.state.session_id}.md"
    resolved = Path(path).expanduser().resolve()
    resolved.relative_to(context.workspace.resolve())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _approval_allows_exit(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return value.get("choice") == "approve"
    return False


def setup_plan(api: ExtensionAPI) -> None:
    async def enter(_value: dict[str, Any], context: ExtensionContext) -> str:
        state = context.require("permission_state")
        if state.mode != "plan":
            state.mode_before_plan = state.mode
            state.pre_plan_tools = frozenset(context.host._tool_names_for(context))
            state.pre_plan_ceiling = context.tool_ceiling_names
        state.mode = "plan"
        base_ceiling = (
            set(context.tool_ceiling_names)
            if context.tool_ceiling_names is not None
            else set(context.host.registered_tool_names())
        )
        context.tool_ceiling_names = frozenset(base_ceiling - {"run_shell"})
        context.active_tool_names = frozenset(
            context.host._tool_names_for(context) - {"run_shell"}
        )
        state.plan_file = _plan_path(context)
        context.append_state(
            "plan",
            {
                "mode": state.mode,
                "mode_before_plan": state.mode_before_plan,
                "plan_file": str(state.plan_file),
            },
        )
        return f"Entered plan mode. The only writable file is {state.plan_file}."

    async def exit_plan(_value: dict[str, Any], context: ExtensionContext) -> str:
        state = context.require("permission_state")
        if state.mode != "plan":
            return "Plan mode is not active."
        if context.depth > 0:
            return "Child agents cannot exit an inherited Plan Mode."
        state.plan_file = state.plan_file or _plan_path(context)
        plan = state.plan_file.read_text(encoding="utf-8", errors="replace") if state.plan_file.exists() else ""
        callback = context.task.runtime.permissions.plan_approval
        if callback is None:
            return "Plan approval is required, but no approval callback is configured."
        result = callback(plan)
        if inspect.isawaitable(result):
            result = await result
        if not _approval_allows_exit(result):
            feedback = result.get("feedback") if isinstance(result, dict) else None
            return (
                f"Plan was not approved. Requested revision: {feedback}"
                if feedback
                else "Plan was not approved. Revise it and request approval again."
            )
        state.mode = state.mode_before_plan or "default"
        context.active_tool_names = state.pre_plan_tools
        context.tool_ceiling_names = state.pre_plan_ceiling
        state.pre_plan_tools = None
        state.pre_plan_ceiling = None
        context.append_state(
            "plan",
            {"mode": state.mode, "mode_before_plan": state.mode_before_plan, "plan_file": str(state.plan_file)},
        )
        return f"Exited plan mode. Approved plan:\n\n{plan}" if plan else "Exited plan mode."

    async def session_start(_event: ExtensionEvent, context: ExtensionContext) -> None:
        state = context.require("permission_state")
        persisted = context.latest_state("plan")
        if state.mode != "plan" and isinstance(persisted, dict):
            state.mode = str(persisted.get("mode") or state.mode)
            state.mode_before_plan = str(persisted.get("mode_before_plan") or state.mode_before_plan)
        if state.mode == "plan":
            state.pre_plan_tools = frozenset(context.host._tool_names_for(context))
            state.pre_plan_ceiling = context.tool_ceiling_names
            base_ceiling = (
                set(context.tool_ceiling_names)
                if context.tool_ceiling_names is not None
                else set(context.host.registered_tool_names())
            )
            context.tool_ceiling_names = frozenset(base_ceiling - {"run_shell"})
            context.active_tool_names = frozenset(
                context.host._tool_names_for(context) - {"run_shell"}
            )
            state.plan_file = _plan_path(context)

    async def command(_args: str, context: ExtensionContext) -> str:
        state = context.require("permission_state")
        return await (exit_plan({}, context) if state.mode == "plan" else enter({}, context))

    def prompt(_render: Any, context: ExtensionContext) -> str:
        state = context.require("permission_state")
        if state.mode != "plan":
            return ""
        return (
            "# Plan mode\n"
            "You are in a hard read-only planning phase. Inspect the workspace and produce a concrete plan. "
            f"Only the plan artifact may be written: `{state.plan_file}`. "
            "Do not delegate to a write-capable role or attempt implementation until the plan is approved."
        )

    api.register_tool(
        {
            "name": "enter_plan_mode",
            "description": "Enter the hard read-only planning phase.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        enter,
        deferred=True,
    )
    api.register_tool(
        {
            "name": "exit_plan_mode",
            "description": "Request approval for the current plan and leave Plan Mode when approved.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        exit_plan,
        deferred=True,
    )
    api.register_command("plan", command, description="Toggle Plan Mode")
    api.contribute_prompt("plan-mode", prompt, priority=10)
    api.on("session_start", session_start)


def setup_mcp(api: ExtensionAPI) -> None:
    manager = McpManager()

    async def session_start(_event: ExtensionEvent, context: ExtensionContext) -> None:
        if context.task.runtime.execution.backend != "local" or context.depth > 0:
            return
        configs = manager.load_configs(context.workspace)
        if not configs:
            return
        decision = await context.authorize(
            "mcp_server_start",
            {"summary": manager.config_summary(configs)},
            call_id="mcp-start",
        )
        if decision.action != "allow":
            return
        await manager.load_and_connect(configs)
        for definition in manager.get_tool_definitions():
            tool_name = str(definition["name"])

            async def call_tool(
                value: dict[str, Any],
                _context: ExtensionContext,
                name: str = tool_name,
            ) -> str:
                return await manager.call_tool(name, value)

            api.register_tool(
                definition,
                call_tool,
                prompt_snippet=f"`{tool_name}` calls a connected external MCP tool.",
            )

    async def shutdown(_event: ExtensionEvent, _context: ExtensionContext) -> None:
        await manager.disconnect_all()

    async def status(_args: str, _context: ExtensionContext) -> str:
        definitions = manager.get_tool_definitions()
        if not definitions:
            return "No MCP servers are connected."
        return "Connected MCP tools:\n" + "\n".join(f"- {item['name']}" for item in definitions)

    api.on("session_start", session_start)
    api.on("session_shutdown", shutdown)
    api.register_command("mcp", status, description="Show connected MCP tools")


__all__ = ["PermissionState", "setup_mcp", "setup_permissions", "setup_plan"]
