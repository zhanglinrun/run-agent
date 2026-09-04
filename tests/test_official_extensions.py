from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from extensions.mcp.extension import McpHttpClient

from run_agent_coding.commands import CommandRegistry
from run_agent_coding.extension_installer import install_extension
from run_agent_coding.extensions.runtime import ExtensionRuntime
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_core import AgentTool, AgentToolResult, AssistantMessage
from run_agent_core.events import AgentEndEvent, AgentStartEvent, TurnEndEvent, TurnStartEvent

EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "extensions"


def _runtime(
    tmp_path: Path,
    *extensions: str,
    environment: dict[str, str] | None = None,
) -> ExtensionRuntime:
    runtime = ExtensionRuntime(
        paths=RunAgentPaths(home=tmp_path / ".run", agents_home=tmp_path / ".agents"),
        environment=environment or {},
    )
    runtime.bind(SimpleNamespace(cwd=tmp_path, session_id="session-1"))  # type: ignore[arg-type]
    runtime.load(
        RunAgentResourcePaths(root=tmp_path / "resources", agents_root=None),
        extra_paths=tuple(EXTENSIONS_DIR / name for name in extensions),
        include_resource_dirs=False,
        include_project_dir=False,
        include_user_dir=False,
    )
    errors = [diagnostic for diagnostic in runtime.diagnostics if diagnostic.severity == "error"]
    assert errors == []
    return runtime


def _tool(tools: tuple[AgentTool, ...] | list[AgentTool], name: str) -> AgentTool:
    return next(tool for tool in tools if tool.name == name)


def test_official_extensions_are_discovered_only_when_explicit(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        "mem0",
        "mcp",
        "observability",
        "permission_policy",
        "plan_mode",
        "verification",
        environment={"MEM0_API_KEY": "test-key"},
    )

    assert runtime.extension_names == (
        "mem0",
        "mcp",
        "observability",
        "permission_policy",
        "plan_mode",
        "verification",
    )
    assert {tool.name for tool in runtime.extension_tools} == {"memory", "verify"}


def test_installed_mem0_extension_is_discovered_from_user_directory(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / ".run", agents_home=tmp_path / ".agents")
    destination = install_extension(
        str(EXTENSIONS_DIR / "mem0"),
        extensions_dir=paths.user_extensions_dir,
    )
    runtime = ExtensionRuntime(
        paths=paths,
        environment={"MEM0_API_KEY": "test-key"},
    )
    runtime.bind(SimpleNamespace(cwd=tmp_path, session_id="session-1"))  # type: ignore[arg-type]

    runtime.load(
        RunAgentResourcePaths(root=paths.home, agents_root=paths.agents_home),
        include_project_dir=False,
    )

    assert destination == paths.user_extensions_dir / "mem0"
    assert runtime.extension_names == ("mem0",)
    assert [tool.name for tool in runtime.extension_tools] == ["memory"]


@pytest.mark.anyio
async def test_mem0_and_plan_mode_compose_as_regular_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v3/memories/add/":
            return httpx.Response(
                200,
                json={"status": "SUCCEEDED", "results": [{"id": "memory-1"}]},
            )
        if request.url.path == "/v3/memories/search/":
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "memory-1", "memory": "Use a project virtual environment"}]
                },
            )
        raise AssertionError(f"Unexpected Mem0 request: {request.method} {request.url}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: http_client)
    runtime = _runtime(
        tmp_path,
        "mem0",
        "plan_mode",
        environment={
            "MEM0_API_KEY": "mem0-test-key",
            "MEM0_BASE_URL": "https://memory.example.test",
        },
    )
    memory = _tool(list(runtime.compose_tools([])), "memory")

    stored = await memory.execute(
        "put", {"action": "put", "text": "Use a project virtual environment"}
    )
    found = await memory.execute("search", {"action": "search", "query": "project environment"})

    assert "Stored Mem0 memory memory-1" in stored.text
    assert "Use a project virtual environment" in found.text
    assert all(request.headers["Authorization"] == "Token mem0-test-key" for request in requests)
    assert json.loads(requests[0].content)["infer"] is False

    command_registry: CommandRegistry = runtime.build_command_registry()
    enabled = command_registry.execute(SimpleNamespace(), "/plan on")  # type: ignore[arg-type]
    blocked = await memory.execute("blocked", {"action": "put", "text": "do not persist"})

    assert enabled.message == "Plan mode enabled. Mutating tools are blocked."
    assert "Tool call blocked" in blocked.text
    assert len(requests) == 2
    await runtime.emit_session_shutdown("quit")


@pytest.mark.anyio
async def test_mem0_extension_reports_missing_configuration(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "mem0")
    memory = _tool(list(runtime.compose_tools([])), "memory")

    with pytest.raises(RuntimeError, match="MEM0_API_KEY"):
        await memory.execute("search", {"action": "search", "query": "anything"})


@pytest.mark.anyio
async def test_observability_extension_writes_agent_trace(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "observability")

    await runtime.emit_event(AgentStartEvent())
    await runtime.emit_event(TurnStartEvent())
    await runtime.emit_event(TurnEndEvent(message=AssistantMessage(content="done")))
    await runtime.emit_event(AgentEndEvent(messages=[]))

    trace_paths = list((tmp_path / ".run" / "traces").glob("*.jsonl"))
    assert len(trace_paths) == 1
    records = [json.loads(line) for line in trace_paths[0].read_text(encoding="utf-8").splitlines()]
    assert [record["name"] for record in records] == ["turn", "agent"]


@pytest.mark.anyio
async def test_permission_policy_extension_blocks_outside_workspace_writes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, "permission_policy")
    executed = False

    async def execute(*args, **kwargs) -> AgentToolResult:  # noqa: ANN002, ANN003
        nonlocal executed
        executed = True
        return AgentToolResult(content="wrote")

    write = AgentTool(
        name="write",
        label="Write",
        description="write",
        parameters={"type": "object"},
        execute_fn=execute,
    )
    wrapped = _tool(list(runtime.compose_tools([write])), "write")

    result = await wrapped.execute("outside", {"path": str(tmp_path.parent / "outside.txt")})

    assert executed is False
    assert "outside the workspace" in result.text


@pytest.mark.anyio
async def test_verification_extension_returns_structured_evidence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "verification")
    verify = _tool(list(runtime.compose_tools([])), "verify")
    command = f"\"{sys.executable}\" -c \"print('verified', end='')\""

    result = await verify.execute("verify", {"command": command, "timeout": 5})

    assert result.text == "verified"
    assert isinstance(result.details, dict)
    assert result.details["exit_code"] == 0
    assert result.details["verified"] is True


@pytest.mark.anyio
async def test_mcp_http_client_initializes_once_and_preserves_session_header() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append({"payload": payload, "session": request.headers.get("Mcp-Session-Id")})
        method = payload["method"]
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
                headers={"Mcp-Session-Id": "session-1"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"content": []}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = McpHttpClient("https://mcp.example.test", http_client)
        await client.list_tools()
        await client.call_tool("echo", {"value": "hi"})

    assert [request["payload"]["method"] for request in requests] == [  # type: ignore[index]
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert [request["session"] for request in requests[1:]] == ["session-1"] * 3
