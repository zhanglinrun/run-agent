"""
MCP client for Run Agent (C07).

Connects stdio MCP servers over JSON-RPC (no MCP SDK), discovers tools, and
routes `mcp__server__tool` calls into the same Agent Loop.

Config (later files override same server name):
- ~/.run/settings.json
- .run/settings.json
- .mcp.json  (Claude Code convention)

Example:
{
  "mcpServers": {
    "demo": {
      "command": "python",
      "args": ["echo_mcp_server.py"]
    }
  }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .ui import print_error, print_info


class McpConnection:
    """One MCP server subprocess + JSON-RPC over stdio."""

    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.server_name = server_name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self) -> None:
        merged_env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if "error" in msg:
                    e = msg["error"]
                    fut.set_exception(
                        RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                    )
                else:
                    fut.set_result(msg.get("result"))

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        assert self._process and self._process.stdin
        req_id = self._next_id
        self._next_id += 1

        msg = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        )
        self._process.stdin.write((msg + "\n").encode())
        await self._process.stdin.drain()

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        return await fut

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "run-agent", "version": "0.1.0"},
            },
        )
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            return "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
        return json.dumps(result)

    def close(self) -> None:
        """Best-effort sync close (used on failed connect). Prefer aclose()."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            self._reader_task = None
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            self._process = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    RuntimeError(f"MCP server '{self.server_name}' closed")
                )
        self._pending.clear()

    async def aclose(self) -> None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                    await self._process.wait()
                except Exception:
                    pass
            self._process = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    RuntimeError(f"MCP server '{self.server_name}' closed")
                )
        self._pending.clear()


class McpManager:
    """Load configs, connect servers, expose prefixed tools to the Agent Loop."""

    def __init__(self) -> None:
        self._connections: dict[str, McpConnection] = {}
        self._tools: list[dict] = []
        self._connected = False

    async def load_and_connect(self) -> None:
        if self._connected:
            return
        self._connected = True

        configs = self._load_configs()
        if not configs:
            return

        timeout = 15.0
        for name, cfg in configs.items():
            conn = McpConnection(
                name,
                cfg["command"],
                cfg.get("args"),
                cfg.get("env"),
            )
            try:
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except Exception as e:
                print_error(f"MCP failed to connect: {name}: {e}")
                conn.close()

    def get_tool_definitions(self) -> list[dict]:
        """Bear-shaped defs (name/description/input_schema) for to_openai_tools()."""
        return [
            {
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description")
                or f"MCP tool {t['name']} from {t['serverName']}",
                "input_schema": t.get("inputSchema")
                or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        parts = prefixed_name.split("__")
        if len(parts) < 3 or parts[0] != "mcp":
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        tool_name = "__".join(parts[2:])
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        return await conn.call_tool(tool_name, args)

    def format_status(self) -> str:
        if not self._connected:
            return "MCP: not initialized yet (will connect on first chat or /mcp)."
        if not self._connections:
            return "MCP: no servers connected (check ~/.run/settings.json, .run/settings.json, .mcp.json)."
        lines = [f"MCP: {len(self._connections)} server(s)"]
        by_server: dict[str, list[str]] = {name: [] for name in self._connections}
        for t in self._tools:
            by_server.setdefault(t["serverName"], []).append(t["name"])
        for name, tools in by_server.items():
            tool_list = ", ".join(tools) if tools else "(no tools)"
            lines.append(f"  - {name}: {tool_list}")
        return "\n".join(lines)

    async def disconnect_all(self) -> None:
        for conn in list(self._connections.values()):
            await conn.aclose()
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    def _load_configs(self) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        self._merge_config_file(Path.home() / ".run" / "settings.json", merged)
        self._merge_config_file(Path.cwd() / ".run" / "settings.json", merged)
        self._merge_config_file(Path.cwd() / ".mcp.json", merged)
        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            servers = raw.get("mcpServers", raw)
            if not isinstance(servers, dict):
                return
            for name, config in servers.items():
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            pass
