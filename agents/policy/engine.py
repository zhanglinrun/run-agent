"""Central permission engine for built-in, shell and MCP actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .shell import ShellRisk, classify_shell_command
from .workspace import WorkspaceBoundary, WorkspaceViolation


READ_TOOLS = {"read_file", "list_files", "grep_search", "compact_context", "skill"}
FILE_TOOLS = {"read_file", "write_file", "edit_file", "list_files", "grep_search"}
EDIT_TOOLS = {"write_file", "edit_file"}
META_TOOLS = {"agent", "tool_search", "enter_plan_mode", "exit_plan_mode"}
SKILL_MUTATION_TOOLS = {"skill_evolve", "skill_create"}
MEMORY_MUTATION_TOOLS = {"memory_save"}


@dataclass(frozen=True)
class PermissionDecision:
    action: str
    message: str = ""
    reason_code: str = ""
    risk: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value != ""}


class PolicyEngine:
    """Evaluate hard invariants before user/project convenience rules."""

    def __init__(self, boundary: WorkspaceBoundary | None = None) -> None:
        self.boundary = boundary or WorkspaceBoundary()

    @staticmethod
    def normalize_name(tool_name: str) -> str:
        return {"grep": "grep_search", "bash": "run_shell"}.get(tool_name, tool_name)

    def _path_decision(self, name: str, inp: dict[str, Any], plan_file_path: str | None) -> PermissionDecision | None:
        if name not in FILE_TOOLS:
            return None
        raw = inp.get("file_path") if name in {"read_file", "write_file", "edit_file"} else inp.get("path", ".")
        if plan_file_path and raw:
            try:
                if self.boundary.resolve(str(raw), must_exist=False) == Path(plan_file_path).expanduser().resolve():
                    return None
            except (OSError, WorkspaceViolation):
                pass
        try:
            self.boundary.resolve(str(raw or "."), must_exist=False)
        except WorkspaceViolation as exc:
            return PermissionDecision("deny", str(exc), "workspace_boundary", "write" if name in EDIT_TOOLS else "read")
        return None

    def decide(
        self,
        tool_name: str,
        inp: dict[str, Any],
        *,
        mode: str = "default",
        plan_file_path: str | None = None,
        rule_result: str | None = None,
    ) -> PermissionDecision:
        name = self.normalize_name(tool_name)

        path_decision = self._path_decision(name, inp, plan_file_path)
        if path_decision:
            return path_decision

        # Plan is a hard invariant.  Neither project allow rules nor yolo-like
        # convenience settings are consulted before this branch.
        if mode == "plan":
            if name in READ_TOOLS or name in {"enter_plan_mode", "exit_plan_mode", "tool_search", "agent"}:
                return PermissionDecision("allow", reason_code="plan_read_only")
            if name in EDIT_TOOLS and plan_file_path and inp.get("file_path"):
                try:
                    if self.boundary.resolve(str(inp["file_path"]), must_exist=False) == Path(plan_file_path).expanduser().resolve():
                        return PermissionDecision("allow", reason_code="plan_file_exception")
                except (OSError, WorkspaceViolation):
                    pass
            return PermissionDecision("deny", f"Blocked in plan mode: {name}", "plan_mode")

        if rule_result == "deny":
            return PermissionDecision("deny", f"Denied by permission rule for {name}", "configured_deny")

        # Workspace boundaries remain active in bypass mode; bypass skips
        # confirmations, not the coding workspace sandbox.
        if mode == "bypassPermissions":
            return PermissionDecision("allow", reason_code="explicit_bypass")

        if name == "mcp_server_start":
            message = str(inp.get("summary") or "start configured MCP servers")
            if mode == "dontAsk":
                return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "mcp_start")
            return PermissionDecision("confirm", message, "mcp_start", "external")

        if name.startswith("mcp__"):
            message = f"call external MCP tool: {name}"
            if mode == "dontAsk":
                return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "external_tool")
            return PermissionDecision("confirm", message, "external_tool", "external")

        if name == "run_shell":
            assessment = classify_shell_command(str(inp.get("command") or ""))
            if assessment.risk in {ShellRisk.READ_ONLY, ShellRisk.VERIFY}:
                if rule_result == "allow" or rule_result is None:
                    return PermissionDecision("allow", assessment.reason, "shell_safe", assessment.risk.value)
            message = f"{assessment.risk.value} shell command: {inp.get('command', '')}"
            if mode == "dontAsk":
                return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "shell_risk", assessment.risk.value)
            # acceptEdits only auto-approves typed file tools.  Shell mutation
            # remains confirmable so it cannot bypass read-before-write.
            return PermissionDecision("confirm", message, "shell_risk", assessment.risk.value)

        if name in READ_TOOLS or name in META_TOOLS:
            return PermissionDecision("allow", reason_code="read_or_meta")

        if name in SKILL_MUTATION_TOOLS | MEMORY_MUTATION_TOOLS:
            target = inp.get("skill_name") or inp.get("name") or ""
            message = f"persist {name}: {target}".strip()
            if mode == "dontAsk":
                return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "managed_mutation")
            return PermissionDecision("confirm", message, "managed_mutation", "write")

        if rule_result == "allow":
            return PermissionDecision("allow", reason_code="configured_allow")

        if name in EDIT_TOOLS:
            if mode == "acceptEdits":
                return PermissionDecision("allow", reason_code="accept_edits", risk="write")
            target = str(inp.get("file_path") or "")
            if name == "write_file" and target:
                try:
                    if not self.boundary.resolve(target).exists():
                        message = f"write new file: {target}"
                        if mode == "dontAsk":
                            return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "new_file")
                        return PermissionDecision("confirm", message, "new_file", "write")
                except (OSError, WorkspaceViolation):
                    pass
            return PermissionDecision("allow", reason_code="existing_file_edit", risk="write")

        message = f"call extension tool: {name}"
        if mode == "dontAsk":
            return PermissionDecision("deny", f"Auto-denied (dontAsk mode): {message}", "extension_tool")
        return PermissionDecision("confirm", message, "extension_tool", "external")
