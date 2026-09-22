"""MCP extension: ``cwd/.mcp.json`` plus per-tool registration."""

from __future__ import annotations

from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
)

from .client import MCPClientManager


def setup(api: ExtensionAPI) -> None:
    """Load MCP servers at session start and release them through one disposer."""
    manager = MCPClientManager()
    registered_tools: list[str] = []
    started = False

    async def on_start(event: object, context: ExtensionContext) -> None:
        nonlocal started
        del event
        if started:
            return
        started = True
        config_path = context.cwd / ".mcp.json"
        if not config_path.exists():
            return
        try:
            server_configs = manager.load_config(config_path)
        except Exception as exc:
            if context.has_ui:
                context.ui.notify(f"Failed to parse .mcp.json: {exc}", level="warning")
            return
        for config in server_configs:
            try:
                tools = await manager.connect_server(config)
                for tool in tools:
                    api.register_tool(tool)
                    registered_tools.append(tool.name)
            except Exception as exc:
                connection = manager.connections.pop(config.name, None)
                if connection is not None:
                    await connection.close()
                if context.has_ui:
                    context.ui.notify(
                        f"MCP connect failed for '{config.name}': {exc}",
                        level="warning",
                    )

    async def dispose() -> None:
        await manager.close_all()

    def mcp_status(args: str, command_context: ExtensionCommandContext) -> str:
        del args, command_context
        if not manager.connections:
            return "No MCP servers connected."
        lines = ["=== MCP servers ==="]
        for name, conn in manager.connections.items():
            status = "Connected" if conn._session is not None else "Disconnected"
            lines.append(f"- {name}: {status} (command: {conn.config.command})")
        lines.append(f"Tools: {', '.join(registered_tools) or '(none)'}")
        return "\n".join(lines)

    api.register_disposer(dispose)
    api.on("session_start", cast(ExtensionHandler, on_start))
    api.register_command(
        "mcp",
        mcp_status,
        description="Show connected MCP servers and tools",
        usage="/mcp",
    )
