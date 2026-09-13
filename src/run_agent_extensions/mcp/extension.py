"""Optional MCP Streamable HTTP bridge implemented as a regular extension."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    SessionShutdownEvent,
)
from run_agent_core.messages import TextContent
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue


class McpHttpClient:
    """Minimal MCP Streamable HTTP client with lazy initialization."""

    def __init__(self, endpoint: str, client: httpx.AsyncClient) -> None:
        self.endpoint = endpoint
        self.client = client
        self.session_id: str | None = None
        self._initialized = False
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def list_tools(self) -> JSONValue:
        await self._ensure_initialized()
        return await self._request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict[str, JSONValue]) -> JSONValue:
        await self._ensure_initialized()
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await self._request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "run-agent-mcp-extension", "version": "1"},
                },
            )
            await self._notify("notifications/initialized", {})
            self._initialized = True

    async def _request(self, method: str, params: dict[str, JSONValue]) -> JSONValue:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        response = await self.client.post(
            self.endpoint,
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        if session_id := response.headers.get("Mcp-Session-Id"):
            self.session_id = session_id
        body = _response_json(response)
        if isinstance(body, dict) and body.get("error") is not None:
            raise RuntimeError(f"MCP {method} failed: {body['error']}")
        return cast(JSONValue, body.get("result") if isinstance(body, dict) else body)

    async def _notify(self, method: str, params: dict[str, JSONValue]) -> None:
        response = await self.client.post(
            self.endpoint,
            json={"jsonrpc": "2.0", "method": method, "params": params},
            headers=self._headers(),
        )
        response.raise_for_status()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return headers


def setup(api: ExtensionAPI) -> None:
    """Register one bridge tool when MCP endpoints are configured."""
    servers = _configured_servers(api.context.environment)
    if not servers:
        return
    http_client = httpx.AsyncClient(timeout=30)
    clients = {name: McpHttpClient(endpoint, http_client) for name, endpoint in servers.items()}

    async def execute(
        tool_call_id: str,
        arguments: dict[str, JSONValue] | Any,
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, on_update
        if signal is not None and signal.is_cancelled():
            return AgentToolResult(content=[TextContent(text="MCP operation cancelled")])
        server_name = str(arguments.get("server", ""))
        client = clients.get(server_name)
        if client is None:
            raise ValueError(f"Unknown MCP server {server_name!r}; available: {', '.join(clients)}")
        action = str(arguments.get("action", "call"))
        if action == "list":
            result = await client.list_tools()
        elif action == "call":
            tool_name = str(arguments.get("tool", ""))
            raw_tool_args = arguments.get("arguments", {})
            if not tool_name or not isinstance(raw_tool_args, dict):
                raise ValueError("MCP call requires tool and object arguments")
            result = await client.call_tool(tool_name, cast(dict[str, JSONValue], raw_tool_args))
        else:
            raise ValueError("MCP action must be list or call")
        return AgentToolResult(
            content=[TextContent(text=json.dumps(result, ensure_ascii=False, indent=2))],
            details={"server": server_name, "action": action},
        )

    async def shutdown(event: SessionShutdownEvent, extension_context: ExtensionContext) -> None:
        del event, extension_context
        await http_client.aclose()

    api.register_tool(
        AgentTool(
            name="mcp",
            label="MCP",
            description="List or invoke tools from configured MCP Streamable HTTP servers.",
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "enum": list(servers)},
                    "action": {"type": "string", "enum": ["list", "call"]},
                    "tool": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["server", "action"],
            },
            execute_fn=execute,
            prompt_snippet="Use configured MCP servers",
        )
    )
    api.on("session_shutdown", cast(ExtensionHandler, shutdown))


def _response_json(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise RuntimeError("MCP server returned an empty event stream")


def _configured_servers(environment: Any) -> dict[str, str]:
    raw = environment.get("RUN_AGENT_MCP_SERVERS", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("RUN_AGENT_MCP_SERVERS must be a JSON object") from exc
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(url, str) for name, url in value.items()
    ):
        raise ValueError("RUN_AGENT_MCP_SERVERS must map server names to URLs")
    return value
