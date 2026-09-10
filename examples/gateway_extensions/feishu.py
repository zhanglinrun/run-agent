"""Feishu long-connection gateway adapter.

Install the optional dependency with ``pip install -e ".[feishu]"`` and set
``FEISHU_APP_ID`` plus ``FEISHU_APP_SECRET`` before loading this extension.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lark_oapi.channel import FeishuChannel, SendOpts
from lark_oapi.ws import client as ws_client_module

from run_agent_core.types import JSONValue
from run_agent_gateway import AdmissionRejected, BoundedIngress, Delivery, InboundMessage
from run_agent_gateway.controller import CONTROL_COMMANDS

GATEWAY_EXTENSION_API_VERSION = 2
GATEWAY_EXTENSION_NAME = "feishu"


class _GatewayFeishuChannel(FeishuChannel):
    """Run the SDK's module-global WebSocket loop on its executor thread."""

    def start(self) -> None:
        previous_ws_loop = ws_client_module.loop
        try:
            previous_thread_loop = asyncio.get_event_loop()
        except RuntimeError:
            previous_thread_loop = None
        websocket_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(websocket_loop)
        ws_client_module.loop = websocket_loop
        try:
            super().start()
        finally:
            pending = asyncio.all_tasks(websocket_loop)
            for task in pending:
                task.cancel()
            if pending and not websocket_loop.is_closed():
                websocket_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            if not websocket_loop.is_closed():
                websocket_loop.close()
            if ws_client_module.loop is websocket_loop:
                ws_client_module.loop = previous_ws_loop
            asyncio.set_event_loop(previous_thread_loop)


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
        self._channel = channel or _GatewayFeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            domain=domain,
            transport="ws",
        )
        self.name = env.get("FEISHU_INSTANCE_ID", "feishu")
        self._account_id = app_id
        self._incoming = BoundedIngress()
        self._ordinary_callbacks = threading.BoundedSemaphore(128)
        self._control_callbacks = threading.BoundedSemaphore(32)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._started = False
        self._unsubscribe = self._channel.on("message", self._on_message)

    def _on_message(self, message: Any) -> None:
        loop = self._loop
        if self._closed or loop is None:
            return
        command = str(getattr(message, "content_text", "")).strip().partition(" ")[0]
        capacity = (
            self._control_callbacks if command in CONTROL_COMMANDS else self._ordinary_callbacks
        )
        if not capacity.acquire(blocking=False):
            logging.getLogger(__name__).warning(
                "Feishu ingress full; message not business-accepted"
            )
            return

        def enqueue() -> None:
            try:
                self._enqueue_message(message)
            finally:
                capacity.release()

        try:
            loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            capacity.release()

    def _enqueue_message(self, message: Any) -> None:
        if self._closed:
            return
        text = str(getattr(message, "content_text", "")).strip()
        if not text:
            return

        conversation = message.conversation
        sender = message.sender
        if getattr(sender, "is_bot", False):
            return
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

        try:
            self._incoming.put(
                InboundMessage(
                    source_message_id=str(message.id),
                    account_id=self._account_id,
                    sender_id=str(getattr(sender, "open_id", "")),
                    chat_id=str(conversation.chat_id),
                    thread_id=str(thread_id or ""),
                    text=text,
                    metadata=metadata,
                )
            )
        except AdmissionRejected:
            logging.getLogger(__name__).warning(
                "Feishu ingress full; message not business-accepted"
            )

    async def messages(self) -> AsyncIterator[InboundMessage]:
        if self._closed:
            return
        if self._started:
            raise RuntimeError("Feishu adapter input stream is already running")
        self._started = True
        self._loop = asyncio.get_running_loop()
        await self._channel.start_background()
        async for message in self._incoming.messages():
            yield message

    async def send(self, delivery: Delivery) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Feishu adapter is closed")
        if delivery.destination.get("account_id") != self._account_id:
            raise ValueError("Delivery belongs to a different Feishu account")
        body = delivery.content
        text = str(
            body.get("output")
            or body.get("error")
            or json.dumps(body, ensure_ascii=False, indent=2)
        )
        chunks = [text[start : start + 1500] for start in range(0, len(text), 1500)]
        message_ids = []
        for index, chunk in enumerate(chunks):
            result = await self._channel.send(
                delivery.destination["chat_id"],
                {"text": chunk},
                SendOpts(
                    reply_to=delivery.destination.get("source_message_id"),
                    reply_in_thread=bool(delivery.destination.get("thread_id")),
                    uuid=str(uuid5(NAMESPACE_URL, f"{delivery.delivery_id}/{index}")),
                    reply_target_gone="fail",
                ),
            )
            if not result.success:
                detail = result.error or "unknown Feishu API error"
                raise RuntimeError(f"Feishu message delivery failed: {detail}")
            message_ids.append(result.message_id)
        return {"message_ids": message_ids}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        await asyncio.to_thread(self._channel.stop)
        self._incoming.close()


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required by the Feishu gateway extension")
    return value


def setup_gateway(api: Any) -> None:
    api.register_adapter(FeishuAdapter())
