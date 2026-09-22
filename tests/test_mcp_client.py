"""Unit tests for my-pi-agent-aligned MCP config loading and lifecycle."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_extensions.mcp.client import MCPClientManager


def test_load_config_reads_mcp_json(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "npx",
                        "args": ["-y", "demo"],
                        "env": {"X": "1"},
                    },
                    "http-only": {"url": "https://example.com"},
                }
            }
        ),
        encoding="utf-8",
    )
    configs = {item.name: item for item in MCPClientManager().load_config(path)}
    assert set(configs) == {"demo"}
    assert configs["demo"].command == "npx"
    assert configs["demo"].args == ["-y", "demo"]
    assert configs["demo"].env == {"X": "1"}


def test_load_config_missing_file(tmp_path: Path) -> None:
    assert MCPClientManager().load_config(tmp_path / ".mcp.json") == []


def test_load_config_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        MCPClientManager().load_config(path)


def test_from_config_file(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"a": {"command": "echo", "args": ["hi"]}}}),
        encoding="utf-8",
    )
    mgr = MCPClientManager.from_config_file(path)
    assert len(mgr._configs) == 1
    assert mgr._configs[0].name == "a"


REPO_MCP_EXTENSION = (
    Path(__file__).resolve().parents[1] / "src" / "run_agent_extensions" / "mcp" / "extension.py"
)


def fake_client_source(log_path: Path) -> str:
    """A drop-in ``.client`` module: same manager API, no subprocesses."""
    return f"""\
from pathlib import Path

LOG = Path({str(log_path)!r})


class MCPServerConfig:
    def __init__(self, name, command):
        self.name = name
        self.command = command


class MCPConnection:
    def __init__(self, config):
        self.config = config
        self._session = object()

    async def close(self):
        self._session = None
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"close:{{self.config.name}}\\n")


class MCPClientManager:
    def __init__(self):
        self.connections = {{}}
        self._tools = []

    def load_config(self, path):
        import json

        config_path = Path(path)
        if not config_path.exists():
            return []
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return [
            MCPServerConfig(name, server.get("command", ""))
            for name, server in data.get("mcpServers", {{}}).items()
        ]

    async def connect_server(self, config):
        self.connections[config.name] = MCPConnection(config)
        return []

    async def close_all(self):
        if not self.connections:
            return
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write("close_all\\n")
        for connection in list(self.connections.values()):
            await connection.close()
        self.connections.clear()
        self._tools.clear()
"""


@pytest.fixture
def mcp_workspace(tmp_path):
    """The real MCP extension entry point over a stubbed client package."""
    log = tmp_path / "mcp.log"
    package = tmp_path / "mcp_stub"
    package.mkdir()
    (package / "extension.py").write_text(
        REPO_MCP_EXTENSION.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (package / "client.py").write_text(fake_client_source(log), encoding="utf-8")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "fake-server"}}}), encoding="utf-8"
    )
    return tmp_path, package, log


async def open_mcp_app(root: Path, package: Path) -> CodingApplication:
    app = await CodingApplication.open(
        replace(options(root), extension_paths=(package,)), provider=ReplyProvider()
    )
    await app.start()
    return app


def close_lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


async def test_mcp_extension_registers_one_disposer_and_closes_connections_once(
    mcp_workspace,
):
    root, package, log = mcp_workspace
    app = await open_mcp_app(root, package)
    runtime = app.session.extension_runtime
    source_id = runtime._extensions[0].source_id
    registered = runtime._disposers._registered[source_id]
    assert len(registered) == 1
    assert inspect.iscoroutinefunction(registered[0][1])
    assert "demo: Connected" in (await app.command("/mcp")).message
    assert close_lines(log) == []

    await app.aclose()
    assert close_lines(log) == ["close_all", "close:demo"]
    # Repeated close and repeated drain never rerun the disposer.
    await app.aclose()
    assert await runtime._disposers.drain() == 0
    assert close_lines(log) == ["close_all", "close:demo"]


async def test_session_shutdown_notification_does_not_close_mcp_connections(mcp_workspace):
    root, package, log = mcp_workspace
    app = await open_mcp_app(root, package)
    try:
        await app.session.extension_runtime.emit_session_shutdown("reload")
        assert close_lines(log) == []
        assert "demo: Connected" in (await app.command("/mcp")).message
    finally:
        await app.aclose()


async def test_failed_reload_and_branch_keep_mcp_connections_usable(mcp_workspace, monkeypatch):
    root, package, log = mcp_workspace
    app = await open_mcp_app(root, package)
    try:
        runtime = app.session.extension_runtime
        events = [event async for event in app.prompt("first")]
        head = events[-1].head_id

        async def publish_failure(*args, **kwargs):
            raise OSError("publish failed")

        monkeypatch.setattr(app.session.host_services, "publish", publish_failure)
        with pytest.raises(OSError, match="publish failed"):
            await app.command("/reload")
        assert app.session.extension_runtime is runtime and runtime.active
        assert "demo: Connected" in (await app.command("/mcp")).message
        assert close_lines(log) == []

        monkeypatch.undo()

        async def fork_failure(*args, **kwargs):
            raise OSError("branch failed")

        monkeypatch.setattr(app.session.storage, "fork", fork_failure)
        with pytest.raises(OSError, match="branch failed"):
            await app.session.branch_to_entry(head)
        assert app.session.extension_runtime is runtime and runtime.active
        assert "demo: Connected" in (await app.command("/mcp")).message
        assert close_lines(log) == []
    finally:
        await app.aclose()
    assert close_lines(log) == ["close_all", "close:demo"]
