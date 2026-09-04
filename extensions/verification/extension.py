"""Optional deterministic verification tool extension."""

from __future__ import annotations

from typing import Any

from run_agent_coding import create_bash_tool
from run_agent_coding.extensions import ExtensionAPI
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue


def setup(api: ExtensionAPI) -> None:
    """Register a structured wrapper around the standard bash tool."""

    async def execute(
        tool_call_id: str,
        arguments: dict[str, JSONValue] | Any,
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            raise ValueError("verify requires a command")
        timeout = arguments.get("timeout")
        bash = create_bash_tool(cwd=api.context.cwd)
        bash_arguments: dict[str, JSONValue] = {
            "command": command,
            "description": "Running deterministic verification",
        }
        if isinstance(timeout, int | float):
            bash_arguments["timeout"] = float(timeout)
        result = await bash.execute(tool_call_id, bash_arguments, signal, on_update)
        details = dict(result.details) if isinstance(result.details, dict) else {}
        details["verified"] = details.get("exit_code") == 0 and not details.get("timed_out")
        return AgentToolResult(content=result.content, details=details)

    api.register_tool(
        AgentTool(
            name="verify",
            label="Verify",
            description=(
                "Run a deterministic verification command and report structured pass/fail evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "exclusiveMinimum": 0},
                },
                "required": ["command"],
            },
            execute_fn=execute,
            execution_mode="sequential",
            prompt_snippet="Run tests, lint, type checks, or other deterministic verification",
        )
    )
    api.add_prompt_guideline(
        "After modifying code, run the narrowest deterministic verification first; "
        "correct failures and rerun before claiming completion."
    )
