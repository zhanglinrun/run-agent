"""Minimal stdio MCP server for C07 demos (JSON-RPC line protocol)."""

from __future__ import annotations

import json
import sys


def reply(msg_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        # notifications have no id
        if msg_id is None:
            continue
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-demo", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            reply(
                msg_id,
                {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo back the text argument (C07 demo only-read probe).",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string", "description": "Text to echo"}
                                },
                                "required": ["text"],
                            },
                        }
                    ]
                },
            )
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            text = args.get("text", "")
            reply(
                msg_id,
                {"content": [{"type": "text", "text": f"echo:{text}"}]},
            )
        else:
            reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
