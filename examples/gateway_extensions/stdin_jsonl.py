"""JSONL stdin/stdout gateway adapter for local integration and smoke tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict
from uuid import uuid4

from run_agent_gateway import InboundMessage, OutboundMessage

GATEWAY_EXTENSION_API_VERSION = 1
GATEWAY_EXTENSION_NAME = "stdin-jsonl"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdio()


class StdinJsonlAdapter:
    name = "stdin-jsonl"

    def __init__(self) -> None:
        self._closed = False
        self._write_lock = asyncio.Lock()

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while not self._closed:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            payload = json.loads(line)
            yield InboundMessage(
                id=str(payload.get("id") or uuid4().hex),
                channel=str(payload.get("channel", self.name)),
                conversation_id=str(payload["conversation_id"]),
                text=str(payload["text"]),
                lane=payload.get("lane", "foreground"),
                session_id=(str(payload["session_id"]) if payload.get("session_id") else None),
                metadata=payload.get("metadata", {}),
            )

    async def send(self, message: OutboundMessage) -> None:
        async with self._write_lock:
            print(json.dumps(asdict(message), ensure_ascii=False), flush=True)

    async def close(self) -> None:
        self._closed = True


def setup_gateway(api) -> None:  # noqa: ANN001
    api.register_adapter(StdinJsonlAdapter())
