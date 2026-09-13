"""Feishu (Lark) adapter over the official ``lark-oapi`` long connection.

Install the optional dependency with ``pip install -e ".[feishu]"``. The adapter listens
for ``im.message.receive_v1`` events through the SDK's WebSocket channel, turns each text
message into a ``MessageEvent``, and sends replies back as Markdown posts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from run_agent_gateway.config import FeishuConfig
from run_agent_gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
    split_message,
)
from run_agent_gateway.platforms.feishu_cards import build_approval_card, parse_approval_action
from run_agent_gateway.platforms.feishu_dedup import SeenMessages
from run_agent_gateway.platforms.feishu_lock import AppInstanceLock
from run_agent_gateway.session import ChatType, SessionSource

logger = logging.getLogger(__name__)

_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+")
_TYPING_REACTION = "Typing"
_DEDUP_SIZE = 2048


def _action_value(action: Any, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value: Any = action
        for part in path:
            value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
            if value is None:
                break
        if value is not None and str(value):
            return str(value)
    return None


def _load_channel_module() -> Any:
    try:
        from lark_oapi import channel
    except ImportError as exc:
        raise RuntimeError(
            'The Feishu gateway needs lark-oapi; install it with `pip install -e ".[feishu]"`'
        ) from exc
    return channel


def create_channel(config: FeishuConfig) -> Any:
    """Build the SDK channel with the WebSocket loop kept on its own thread."""
    module = _load_channel_module()
    from lark_oapi.ws import client as ws_client_module

    class GatewayFeishuChannel(module.FeishuChannel):  # type: ignore[misc,name-defined]
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
                    websocket_loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                if not websocket_loop.is_closed():
                    websocket_loop.close()
                if ws_client_module.loop is websocket_loop:
                    ws_client_module.loop = previous_ws_loop
                asyncio.set_event_loop(previous_thread_loop)

    return GatewayFeishuChannel(
        app_id=config.app_id,
        app_secret=config.app_secret,
        domain=config.domain,
        transport="ws",
    )


class FeishuAdapter(BasePlatformAdapter):
    name = "feishu"

    def __init__(
        self,
        config: FeishuConfig,
        *,
        channel: Any | None = None,
        group_sessions_per_user: bool = True,
        thread_sessions_per_user: bool = False,
        busy_input_mode: str = "queue",
        busy_queue_max_pending: int = 32,
        dedup_path: Path | None = None,
        lock_path: Path | None = None,
    ) -> None:
        super().__init__(
            group_sessions_per_user=group_sessions_per_user,
            thread_sessions_per_user=thread_sessions_per_user,
            busy_input_mode=busy_input_mode,
            busy_queue_max_pending=busy_queue_max_pending,
        )
        self.config = config
        self.max_message_length = config.max_message_length
        self._channel = channel
        self._owns_channel = channel is None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Any = None
        self._connected = False
        self._instance_lock = AppInstanceLock(lock_path) if lock_path is not None else None
        self._seen_store = (
            SeenMessages(
                dedup_path,
                size=config.dedup_cache_size,
                ttl_seconds=config.dedup_ttl_seconds,
            )
            if dedup_path is not None
            else None
        )
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_lock = threading.Lock()
        self._inbound: set[asyncio.Task[None]] = set()
        self._reactions: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -- lifecycle ------------------------------------------------------------------

    async def connect(self) -> bool:
        if self.config.connection_mode != "websocket":
            raise RuntimeError(
                "This gateway build supports only Feishu WebSocket text delivery; "
                "webhook mode is not implemented"
            )
        if self.config.media_enabled or self.config.streaming or self.config.tool_progress != "off":
            raise RuntimeError(
                "This gateway build supports text-only Feishu messages; disable media, "
                "streaming and tool progress"
            )
        if self._instance_lock is not None:
            acquired, existing = self._instance_lock.acquire()
            if not acquired:
                pid = existing.get("pid") if existing else "unknown"
                raise RuntimeError(f"Another Feishu gateway already owns this app (pid={pid})")
        self._loop = asyncio.get_running_loop()
        if self._channel is None:
            self._channel = create_channel(self.config)
        self._unsubscribe = self._channel.on("message", self._on_message)
        message_unsubscribe = self._unsubscribe

        def no_card_unsubscribe() -> None:
            return None

        card_unsubscribe: Any = no_card_unsubscribe
        try:
            card_unsubscribe = self._channel.on("cardAction", self._on_card_action)
        except (AttributeError, AssertionError, TypeError):
            logger.debug("[feishu] channel has no card action subscription")
        self._unsubscribe = lambda: (message_unsubscribe(), card_unsubscribe())
        await self._channel.start_background()
        self._connected = True
        logger.info("[feishu] connected (%s)", self.config.domain or "feishu.cn")
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._inbound):
            task.cancel()
        if self._inbound:
            await asyncio.gather(*self._inbound, return_exceptions=True)
        await self.cancel_background_tasks()
        if self._channel is not None and self._owns_channel:
            await asyncio.to_thread(self._channel.stop)
        if self._instance_lock is not None:
            self._instance_lock.release()

    async def _on_card_action(self, action: Any) -> None:
        parsed = parse_approval_action(getattr(getattr(action, "action", None), "value", None))
        if parsed is None:
            return
        approval_id, choice = parsed
        operator = getattr(action, "operator", None)
        await self.resolve_approval(
            approval_id,
            choice,
            str(getattr(operator, "open_id", "") or "") or None,
            _action_value(
                action,
                ("context", "open_chat_id"),
                ("context", "chat_id"),
                ("message", "chat_id"),
                ("open_chat_id",),
                ("chat_id",),
            ),
            _action_value(
                action,
                ("context", "open_thread_id"),
                ("context", "thread_id"),
                ("message", "thread_id"),
                ("open_thread_id",),
                ("thread_id",),
            ),
        )

    async def send_approval_card(
        self,
        chat_id: str,
        *,
        approval_id: str,
        command: str,
        description: str,
        allow_session: bool = True,
        allow_always: bool = True,
    ) -> SendResult:
        if self._channel is None:
            return SendResult(success=False, error="Feishu channel is not connected")
        module = _load_channel_module()
        result = await self._channel.send(
            chat_id,
            {
                "interactive": build_approval_card(
                    approval_id=approval_id,
                    command=command,
                    description=description,
                    allow_session=allow_session,
                    allow_always=allow_always,
                )
            },
            module.SendOpts(),
        )
        if not getattr(result, "success", False):
            return SendResult(
                success=False,
                error=str(getattr(result, "error", None) or "Feishu approval card failed"),
                error_kind="format",
            )
        return SendResult(success=True, message_id=getattr(result, "message_id", None))

    # -- inbound --------------------------------------------------------------------

    def _on_message(self, message: Any) -> None:
        """SDK callback; may run on the SDK's thread, so hop onto our loop."""
        loop = self._loop
        if loop is None or not self._connected:
            return
        try:
            loop.call_soon_threadsafe(self._accept, message)
        except RuntimeError:
            logger.debug("[feishu] event loop closed; dropping inbound message")

    def _accept(self, message: Any) -> None:
        event = self.build_event(message)
        if event is None:
            return
        task = asyncio.create_task(self.handle_message(event), name="feishu-inbound")
        self._inbound.add(task)
        task.add_done_callback(self._inbound.discard)

    def build_event(self, message: Any) -> MessageEvent | None:
        """Normalize one SDK message; None means it is not for the agent."""
        message_id = str(getattr(message, "id", "") or "")
        if message_id and not self._first_sight(message_id):
            return None
        sender = getattr(message, "sender", None)
        if sender is None or getattr(sender, "is_bot", False):
            return None
        conversation = message.conversation
        raw_type = str(getattr(conversation, "chat_type", "") or "")
        chat_type: ChatType = "dm" if raw_type in {"p2p", "dm", "private"} else "group"
        mentioned = bool(getattr(message, "mentioned_bot", False))
        if (
            chat_type == "group"
            and self.config.require_mention_for(str(getattr(conversation, "chat_id", "") or ""))
            and not mentioned
        ):
            return None
        text = _MENTION_PLACEHOLDER.sub("", str(getattr(message, "content_text", "") or "")).strip()
        if not text:
            return None
        thread_id = getattr(conversation, "thread_id", None)
        source = SessionSource(
            platform=self.name,
            chat_id=str(conversation.chat_id),
            chat_type=chat_type,
            user_id=str(getattr(sender, "open_id", "") or "") or None,
            user_name=str(getattr(sender, "display_name", "") or "") or None,
            thread_id=str(thread_id) if thread_id else None,
            message_id=message_id or None,
        )
        content = getattr(message, "content", None)
        metadata: dict[str, Any] = {
            "mentioned_bot": mentioned,
            "message_type": str(
                getattr(content, "kind", None) or getattr(message, "raw_content_type", "") or "text"
            ),
        }
        return MessageEvent(
            text=text, source=source, message_id=message_id or None, metadata=metadata
        )

    def _first_sight(self, message_id: str) -> bool:
        if self._seen_store is not None:
            return self._seen_store.first_sight(message_id)
        with self._seen_lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            while len(self._seen) > _DEDUP_SIZE:
                self._seen.popitem(last=False)
            return True

    # -- outbound -------------------------------------------------------------------

    def _chunk_reply_to(self, reply_to: str | None, index: int) -> str | None:
        mode = self.config.reply_to_mode
        return reply_to if mode == "all" or (mode == "first" and index == 0) else None

    async def _send_chunk(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        if self._channel is None:
            return SendResult(success=False, error="Feishu channel is not connected")
        module = _load_channel_module()
        options = module.SendOpts(
            reply_to=reply_to,
            reply_in_thread=bool(thread_id) if reply_to else None,
            reply_target_gone="fresh",
        )
        result = await self._channel.send(chat_id, {"markdown": content}, options)
        if not getattr(result, "success", False):
            return SendResult(
                success=False,
                error=str(getattr(result, "error", None) or "Feishu send failed"),
            )
        return SendResult(success=True, message_id=getattr(result, "message_id", None))

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        """Send directly for adapter callers; Base delivers with per-chunk ledger rows."""
        chunks = split_message(content, self.max_message_length)
        last = SendResult(success=True)
        for index, chunk in enumerate(chunks):
            last = await self._send_chunk(
                chat_id,
                chunk,
                reply_to=self._chunk_reply_to(reply_to, index),
                thread_id=thread_id,
            )
            if not last.success:
                return last
        return last

    async def send_typing(self, event: MessageEvent) -> None:
        if self._channel is None or not event.message_id:
            return
        result = await self._channel.add_reaction(event.message_id, _TYPING_REACTION)
        raw = getattr(result, "raw", None) or {}
        data = raw.get("data") if isinstance(raw, dict) else None
        reaction_id = data.get("reaction_id") if isinstance(data, dict) else None
        if getattr(result, "success", False) and reaction_id:
            self._reactions[event.message_id] = str(reaction_id)

    async def stop_typing(self, event: MessageEvent) -> None:
        if self._channel is None or not event.message_id:
            return
        reaction_id = self._reactions.pop(event.message_id, None)
        if reaction_id:
            await self._channel.remove_reaction(event.message_id, reaction_id)


__all__ = ["FeishuAdapter", "create_channel"]
