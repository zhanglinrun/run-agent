"""Unit tests for my-pi-agent-aligned MCP config loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
