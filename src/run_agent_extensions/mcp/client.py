"""Stdio MCP client aligned with my-pi-agent (``.mcp.json`` + raw tool names)."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from run_agent_core.messages import TextContent
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


def _schema_as_mapping(schema: object) -> dict[str, JSONValue]:
    if isinstance(schema, Mapping):
        return dict(schema)
    dump = getattr(schema, "model_dump", None)
    if callable(dump):
        data = dump(mode="json")
        if isinstance(data, dict):
            return data
    return {"type": "object", "properties": {}}


class MCPConnection:
    """One stdio MCP server session (my-pi-agent ``MCPConnection``)."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._session: ClientSession | None = None
        self._exit_stack = contextlib.AsyncExitStack()

    async def start(self) -> None:
        server_env = os.environ.copy()
        if self.config.env:
            server_env.update(self.config.env)
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=server_env,
        )
        read_stream, write_stream = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        self._session = session
        await session.initialize()

    async def list_tools(self) -> list[mcp_types.Tool]:
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not connected")
        result = await self._session.list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> AgentToolResult:
        if self._session is None:
            return AgentToolResult(
                content=[TextContent(text=f"MCP server '{self.config.name}' is not connected")],
                details={"server": self.config.name, "error": True},
            )
        try:
            call_res = await self._session.call_tool(name=name, arguments=arguments)
        except Exception as exc:
            return AgentToolResult(
                content=[TextContent(text=f"MCP tool '{name}' failed: {exc}")],
                details={"server": self.config.name, "error": True},
            )
        texts: list[str] = []
        for content in call_res.content:
            text_val = getattr(content, "text", None)
            texts.append(str(text_val) if text_val is not None else str(content))
        out_text = "\n".join(texts) or "(no output)"
        is_err = bool(getattr(call_res, "is_error", getattr(call_res, "isError", False)))
        return AgentToolResult(
            content=[TextContent(text=out_text)],
            details={"server": self.config.name, "error": is_err},
        )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._exit_stack.aclose()
        self._session = None


class MCPClientManager:
    """Multi-server manager (my-pi-agent ``MCPClientManager``)."""

    def __init__(self) -> None:
        self.connections: dict[str, MCPConnection] = {}
        self._tools: list[AgentTool] = []
        self._configs: list[MCPServerConfig] = []

    @classmethod
    def from_config_file(cls, path: Path | str) -> MCPClientManager:
        mgr = cls()
        mgr._configs = mgr.load_config(path)
        return mgr

    def load_config(self, path: Path | str) -> list[MCPServerConfig]:
        """Read ``.mcp.json`` (``mcpServers`` → stdio ``command``/``args``/``env``)."""
        config_path = Path(path)
        if not config_path.exists():
            return []
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid JSON in {config_path}: {exc}") from exc
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            return []
        configs: list[MCPServerConfig] = []
        for name, srv in servers.items():
            if not isinstance(name, str) or not isinstance(srv, dict):
                continue
            command = srv.get("command", "")
            if not command:
                continue
            args = srv.get("args", [])
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                args = []
            env_raw = srv.get("env")
            env: dict[str, str] | None = None
            if isinstance(env_raw, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in env_raw.items()
            ):
                env = dict(env_raw)
            configs.append(
                MCPServerConfig(name=name, command=str(command), args=list(args), env=env)
            )
        self._configs = configs
        return configs

    async def connect_server(self, config: MCPServerConfig) -> list[AgentTool]:
        conn = MCPConnection(config)
        await conn.start()
        self.connections[config.name] = conn
        mcp_tools = await conn.list_tools()
        wrapped_tools: list[AgentTool] = []
        for remote in mcp_tools:
            remote_name = remote.name
            schema = _schema_as_mapping(
                getattr(remote, "input_schema", None) or getattr(remote, "inputSchema", None) or {}
            )
            if schema.get("type") is None:
                schema = {
                    **schema,
                    "type": "object",
                    "properties": schema.get("properties") or {},
                }

            def _make_execute(target_conn: MCPConnection, target_name: str) -> Any:
                async def execute(
                    tool_call_id: str,
                    arguments: Mapping[str, JSONValue] | Any,
                    signal: ToolCancellationToken | None = None,
                    on_update: ToolUpdateCallback | None = None,
                ) -> AgentToolResult:
                    del tool_call_id, on_update
                    if signal is not None and signal.is_cancelled():
                        return AgentToolResult(
                            content=[TextContent(text="MCP operation cancelled")]
                        )
                    return await target_conn.call_tool(target_name, dict(arguments))

                return execute

            wrapped = AgentTool(
                name=remote_name,
                label=remote_name,
                description=remote.description or "",
                parameters=schema,
                execute_fn=_make_execute(conn, remote_name),
                prompt_snippet=remote.description or remote_name,
            )
            wrapped_tools.append(wrapped)
        self._tools.extend(wrapped_tools)
        return wrapped_tools

    async def connect_all(self, configs: list[MCPServerConfig] | None = None) -> list[AgentTool]:
        target = configs if configs is not None else self._configs
        tools: list[AgentTool] = []
        for config in target:
            tools.extend(await self.connect_server(config))
        return tools

    def get_all_tools(self) -> list[AgentTool]:
        return list(self._tools)

    async def close_all(self) -> None:
        for conn in self.connections.values():
            with contextlib.suppress(Exception):
                await conn.close()
        self.connections.clear()
        self._tools.clear()
