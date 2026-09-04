"""Feishu long-connection gateway adapter.

Install the optional dependency with ``pip install -e ".[feishu]"`` and set
``FEISHU_APP_ID`` plus ``FEISHU_APP_SECRET`` before loading this extension.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from lark_oapi.channel import FeishuChannel, SendOpts

from run_agent_core.types import JSONValue
from run_agent_gateway import InboundMessage, OutboundMessage

GATEWAY_EXTENSION_API_VERSION = 1
GATEWAY_EXTENSION_NAME = "feishu"


class FeishuAdapter:
    """Bridge Feishu's WebSocket channel to the Run Agent Gateway protocol."""

    name = "feishu"

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        channel: Any | None = None,
    ) -> None:
        env = os.environ if environment is None else environment
        app_id = _required(env, "FEISHU_APP_ID")
        app_secret = _required(env, "FEISHU_APP_SECRET")
        domain = env.get("FEISHU_DOMAIN") or None
        self._channel = channel or FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            domain=domain,
            transport="ws",
        )
        self._incoming: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._started = False
        self._unsubscribe = self._channel.on("message", self._on_message)

    def _on_message(self, message: Any) -> None:
        loop = self._loop
        if self._closed or loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue_message, message)

    def _enqueue_message(self, message: Any) -> None:
        if self._closed:
            return
        text = str(getattr(message, "content_text", "")).strip()
        if not text:
            return

        conversation = message.conversation
        sender = message.sender
        content = getattr(message, "content", None)
        metadata: dict[str, JSONValue] = {
            "chat_type": str(getattr(conversation, "chat_type", "unknown")),
            "sender_id": str(getattr(sender, "open_id", "")),
            "message_type": str(
                getattr(content, "kind", None) or getattr(message, "raw_content_type", "unknown")
            ),
            "mentioned_bot": bool(getattr(message, "mentioned_bot", False)),
        }
        sender_name = getattr(sender, "display_name", None)
        if sender_name:
            metadata["sender_name"] = str(sender_name)
        thread_id = getattr(conversation, "thread_id", None)
        if thread_id:
            metadata["thread_id"] = str(thread_id)

        self._incoming.put_nowait(
            InboundMessage(
                id=str(message.id),
                channel=self.name,
                conversation_id=str(conversation.chat_id),
                text=text,
                metadata=metadata,
            )
        )

    async def messages(self) -> AsyncIterator[InboundMessage]:
        if self._closed:
            return
        if self._started:
            raise RuntimeError("Feishu adapter input stream is already running")
        self._started = True
        self._loop = asyncio.get_running_loop()
        await self._channel.start_background()
        while True:
            message = await self._incoming.get()
            if message is None:
                return
            yield message

    async def send(self, message: OutboundMessage) -> None:
        if self._closed:
            raise RuntimeError("Feishu adapter is closed")
        result = await self._channel.send(
            message.conversation_id,
            {"markdown": message.text},
            SendOpts(
                reply_to=message.request_id,
                reply_target_gone="fresh",
            ),
        )
        if not result.success:
            detail = result.error or "unknown Feishu API error"
            raise RuntimeError(f"Feishu message delivery failed: {detail}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        await asyncio.to_thread(self._channel.stop)
        self._incoming.put_nowait(None)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required by the Feishu gateway extension")
    return value


def setup_gateway(api: Any) -> None:
    api.register_adapter(FeishuAdapter())
