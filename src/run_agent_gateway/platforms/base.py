"""The adapter contract every channel implements, plus per-chat turn serialization.

An adapter turns platform messages into ``MessageEvent`` values and hands them to
``handle_message``. The base class makes sure one chat runs one turn at a time: while a
turn is active, further text for the same chat is collected and processed as the next
turn, and a few control commands (``/stop``, ``/new``) are dispatched immediately so a
user can interrupt. Replies are sent back through the adapter's ``send`` with retries.

The event and result shapes follow hermes-agent's ``gateway/platforms/base.py`` so the
runner can reason about media, reply context and delivery failures the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from run_agent_gateway.ledger import DeliveryLedger
from run_agent_gateway.session import SessionSource, build_session_key

logger = logging.getLogger(__name__)

MessageHandler = Callable[["MessageEvent"], Awaitable[str | None]]
BusyHandler = Callable[["MessageEvent", str], Awaitable[bool]]
ApprovalHandler = Callable[[str, str, str | None, str | None, str | None], Awaitable[None]]

MessageType = Literal["text", "photo", "document", "audio", "video", "sticker", "system"]

# Machine-readable send-failure categories, platform-neutral so the runner can branch
# on them instead of substring-matching provider text.
SendErrorKind = Literal[
    "network", "timeout", "rate_limited", "format", "not_found", "permission", "unknown"
]

# Commands that must reach the runner even while the chat's turn is still running.
BYPASS_WHEN_BUSY = frozenset({"stop", "new", "reset", "status", "help"})

# Error text that marks a transient network failure worth retrying.
RETRYABLE_ERROR_PATTERNS = (
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionrefused",
    "connecttimeout",
    "network",
    "broken pipe",
    "remotedisconnected",
    "eoferror",
)


def is_retryable_error(error: str | None) -> bool:
    """Whether an error string looks like a transient network failure."""
    if not error:
        return False
    lowered = error.lower()
    return any(pattern in lowered for pattern in RETRYABLE_ERROR_PATTERNS)


def is_timeout_error(error: str | None) -> bool:
    """Whether an error string is a read/write timeout.

    A timeout is not retried and does not fall back to plain text: the message may
    already have been delivered.
    """
    if not error:
        return False
    lowered = error.lower()
    return "timed out" in lowered or "readtimeout" in lowered or "writetimeout" in lowered


@dataclass(slots=True)
class MessageEvent:
    """One inbound message, normalized the same way for every channel."""

    text: str
    source: SessionSource
    message_id: str | None = None
    message_type: MessageType = "text"
    # Media the adapter downloaded for the model: local paths and their MIME types,
    # index-aligned.
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    # Reply context, when the user replied to an earlier message.
    reply_to_message_id: str | None = None
    reply_to_text: str | None = None
    reply_to_author_id: str | None = None
    reply_to_author_name: str | None = None
    reply_to_is_own_message: bool = False
    # A per-chat ephemeral system prompt, applied at request time and never persisted.
    channel_prompt: str | None = None
    # When the platform says the message was sent (epoch seconds); now when unknown.
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Set for synthetic events the gateway raises itself (heartbeats); they skip
    # sender authorization and never carry a message to reply to.
    internal: bool = False
    # Whether this event may resolve gateway commands or pending control prompts.
    # Untrusted payloads (webhooks, plugin injections) set this to False so their
    # text stays conversational input.
    allow_gateway_control: bool = True

    def is_command(self) -> bool:
        return (
            self.allow_gateway_control and not self.internal and self.text.lstrip().startswith("/")
        )

    def get_command(self) -> str | None:
        if not self.is_command():
            return None
        head = self.text.lstrip().split(maxsplit=1)[0][1:].lower()
        if not head or "/" in head:
            return None
        return head.split("@", 1)[0]

    def get_command_args(self) -> str:
        if not self.is_command():
            return self.text
        parts = self.text.lstrip().split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        # Phone keyboards turn -- into an em dash and - into an en dash.
        return args.replace("——", "--").replace("—", "--").replace("–", "-")

    @property
    def has_media(self) -> bool:
        return bool(self.media_urls)


@dataclass(frozen=True, slots=True)
class SendResult:
    success: bool
    message_id: str | None = None
    error: str | None = None
    # True for transient connection errors; the base retries those automatically.
    retryable: bool = False
    # A server-requested delay in seconds; honoured over the default backoff once.
    retry_after: float | None = None
    error_kind: SendErrorKind | None = None
    raw: Any = None


class StreamHandle(Protocol):
    """One in-progress draft reply on a platform that can edit a sent message."""

    async def update(self, text: str) -> None:
        """Replace the draft with the text so far; throttled by the adapter."""
        ...

    async def finish(self, text: str) -> SendResult:
        """Deliver the final text and close the draft."""
        ...

    async def abort(self) -> None:
        """Drop the draft; the caller will send the reply through ``send``."""
        ...


class BasePlatformAdapter(ABC):
    name: str = "platform"
    max_message_length: int = 4000

    def __init__(
        self,
        *,
        group_sessions_per_user: bool = True,
        thread_sessions_per_user: bool = False,
        send_attempts: int = 3,
        busy_input_mode: str = "queue",
        busy_queue_max_pending: int = 32,
    ) -> None:
        self.group_sessions_per_user = group_sessions_per_user
        self.thread_sessions_per_user = thread_sessions_per_user
        self.send_attempts = max(1, send_attempts)
        self.busy_input_mode = busy_input_mode
        self.busy_queue_max_pending = max(1, busy_queue_max_pending)
        self._handler: MessageHandler | None = None
        self._busy_handler: BusyHandler | None = None
        # Called with (approval_id, choice, user_id) when a user answers an approval
        # card; ``choice`` is one of once / session / always / deny.
        self._approval_handler: ApprovalHandler | None = None
        self.ledger: DeliveryLedger | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, list[MessageEvent]] = {}
        self._direct: set[asyncio.Task[None]] = set()
        self._transitions: dict[str, int] = {}
        self._closing = False

    # -- contract -------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult: ...

    # -- optional capabilities --------------------------------------------------------

    @property
    def supports_editing(self) -> bool:
        """Whether ``edit_message`` works; progress bubbles need it."""
        return False

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> SendResult:
        """Replace a sent message's text in place; optional for a platform."""
        return SendResult(success=False, error="editing is not supported", error_kind="format")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Remove a sent message; optional for a platform."""
        return False

    async def send_image(
        self, chat_id: str, path: str, *, caption: str = "", reply_to: str | None = None
    ) -> SendResult:
        return SendResult(success=False, error="images are not supported", error_kind="format")

    async def send_document(
        self, chat_id: str, path: str, *, caption: str = "", reply_to: str | None = None
    ) -> SendResult:
        return SendResult(success=False, error="files are not supported", error_kind="format")

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
        """Show an approval prompt with buttons; the text fallback is the runner's job."""
        return SendResult(success=False, error="cards are not supported", error_kind="format")

    @property
    def supports_streaming(self) -> bool:
        """Whether ``start_stream`` returns a handle; draft streaming needs it."""
        return False

    async def start_stream(
        self, chat_id: str, *, reply_to: str | None = None, thread_id: str | None = None
    ) -> StreamHandle | None:
        """Open a draft message that ``update`` rewrites and ``finish`` completes."""
        return None

    async def send_typing(self, event: MessageEvent) -> None:
        """Show that the agent is working; optional for a platform."""
        return None

    async def stop_typing(self, event: MessageEvent) -> None:
        """Clear the working indicator; optional for a platform."""
        return None

    # -- dispatch -------------------------------------------------------------------

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_busy_handler(self, handler: BusyHandler | None) -> None:
        """Install the runner callback used by the steer busy policy."""
        self._busy_handler = handler

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def begin_transition(self, session_key: str) -> None:
        self._transitions[session_key] = self._transitions.get(session_key, 0) + 1

    def end_transition(self, session_key: str) -> None:
        count = self._transitions.get(session_key, 0)
        if count <= 1:
            self._transitions.pop(session_key, None)
        else:
            self._transitions[session_key] = count - 1

    def _queue_pending_event(self, event: MessageEvent, session_key: str) -> None:
        pending = self._pending.setdefault(session_key, [])
        if len(pending) >= self.busy_queue_max_pending:
            task = asyncio.create_task(
                self._deliver_reply_discard(
                    session_key,
                    event.source.chat_id,
                    "当前会话排队已满，请稍后重试。",
                    reply_to=event.message_id,
                    thread_id=event.source.thread_id,
                ),
                name=f"gateway-queue-full:{session_key}",
            )
            self._direct.add(task)
            task.add_done_callback(self._direct.discard)
            return
        pending.append(event)
        logger.debug("[%s] queued follow-up for session %s", self.name, session_key)

    async def resolve_approval(
        self,
        approval_id: str,
        choice: str,
        user_id: str | None,
        chat_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Hand a card answer to the runner; adapters call this from their callbacks."""
        if self._approval_handler is not None:
            await self._approval_handler(approval_id, choice, user_id, chat_id, thread_id)

    def session_key_for(self, source: SessionSource) -> str:
        return build_session_key(
            source,
            group_sessions_per_user=self.group_sessions_per_user,
            thread_sessions_per_user=self.thread_sessions_per_user,
        )

    def is_busy(self, session_key: str) -> bool:
        task = self._active.get(session_key)
        return task is not None and not task.done()

    def pending_count(self, session_key: str) -> int:
        return len(self._pending.get(session_key, ()))

    def pending_events(self, session_key: str) -> tuple[MessageEvent, ...]:
        return tuple(self._pending.get(session_key, ()))

    def clear_pending(self, session_key: str) -> tuple[MessageEvent, ...]:
        """Remove queued inputs before a session replacement can start them."""
        return tuple(self._pending.pop(session_key, ()))

    async def cancel_active(self, session_key: str, *, timeout: float = 10.0) -> bool:
        """Cancel one active turn and report whether it actually stopped."""
        task = self._active.get(session_key)
        if task is None or task.done():
            return True
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout))
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            pass
        return True

    async def wait_for_idle(self, session_key: str) -> None:
        """Wait until this chat has drained its active turn and queued follow-ups."""
        while True:
            task = self._active.get(session_key)
            if task is None:
                return
            await asyncio.gather(task, return_exceptions=True)

    async def handle_message(self, event: MessageEvent) -> None:
        """Accept one inbound message; returns as soon as it is scheduled."""
        if self._handler is None:
            return
        key = self.session_key_for(event.source)
        command = event.get_command()
        if key in self._transitions and command not in BYPASS_WHEN_BUSY:
            self._queue_pending_event(event, key)
            return
        if self.is_busy(key):
            command = event.get_command()
            if command in BYPASS_WHEN_BUSY:
                task = asyncio.create_task(self._dispatch(event), name=f"gateway-control:{key}")
                self._direct.add(task)
                task.add_done_callback(self._direct.discard)
                return
            if self.busy_input_mode == "interrupt":
                task = asyncio.create_task(
                    self._interrupt_and_start(event, key), name=f"gateway-interrupt:{key}"
                )
                self._direct.add(task)
                task.add_done_callback(self._direct.discard)
                return
            if self.busy_input_mode == "steer" and self._busy_handler is not None:
                task = asyncio.create_task(
                    self._steer_or_queue(event, key), name=f"gateway-steer:{key}"
                )
                self._direct.add(task)
                task.add_done_callback(self._direct.discard)
                return
            pending = self._pending.setdefault(key, [])
            if len(pending) >= self.busy_queue_max_pending:
                task = asyncio.create_task(
                    self._deliver_reply_discard(
                        key,
                        event.source.chat_id,
                        "当前会话排队已满，请稍后重试。",
                        reply_to=event.message_id,
                        thread_id=event.source.thread_id,
                    ),
                    name=f"gateway-queue-full:{key}",
                )
                self._direct.add(task)
                task.add_done_callback(self._direct.discard)
                return
            pending.append(event)
            logger.debug("[%s] queued follow-up for busy session %s", self.name, key)
            return
        self._start(event, key)

    async def _deliver_reply_discard(self, *args: Any, **kwargs: Any) -> None:
        await self.deliver_reply(*args, **kwargs)

    async def _interrupt_and_start(self, event: MessageEvent, key: str) -> None:
        current = self._active.get(key)
        if current is not None and not current.done():
            current.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current
        if self._handler is not None and not self.is_busy(key):
            self._start(event, key)

    async def _steer_or_queue(self, event: MessageEvent, key: str) -> None:
        handled = False
        if self._busy_handler is not None:
            try:
                handled = await self._busy_handler(event, key)
            except Exception:
                logger.exception("[%s] steer handler failed", self.name)
        if handled:
            return
        pending = self._pending.setdefault(key, [])
        if len(pending) >= self.busy_queue_max_pending:
            await self.deliver_reply(
                key,
                event.source.chat_id,
                "当前会话排队已满，请稍后重试。",
                reply_to=event.message_id,
                thread_id=event.source.thread_id,
            )
            return
        pending.append(event)
        logger.debug("[%s] steer unavailable; queued follow-up for %s", self.name, key)

    def _start(self, event: MessageEvent, key: str) -> None:
        task = asyncio.create_task(self._process(event, key), name=f"gateway-turn:{key}")
        self._active[key] = task

    async def _process(self, event: MessageEvent, key: str) -> None:
        try:
            await self._dispatch(event)
        finally:
            self._active.pop(key, None)
            pending = self._pending.get(key)
            if pending and key not in self._transitions and self._handler is not None:
                self._start(pending.pop(0), key)
                if not pending:
                    self._pending.pop(key, None)

    async def _dispatch(self, event: MessageEvent) -> None:
        handler = self._handler
        if handler is None:
            return
        key = self.session_key_for(event.source)
        await self._quiet(self.send_typing(event), "send_typing")
        try:
            response = await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[%s] message handler failed", self.name)
            response = f"处理消息时出错：{exc}"
        finally:
            await self._quiet(self.stop_typing(event), "stop_typing")
        if response:
            await self.deliver_reply(
                key,
                event.source.chat_id,
                response,
                reply_to=event.message_id,
                thread_id=event.source.thread_id,
            )

    async def deliver_reply(
        self,
        session_key: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        ledgered: bool = True,
    ) -> SendResult:
        """Persist and deliver each platform-sized reply chunk independently."""
        chunks = split_message(content, self.max_message_length)
        if not chunks:
            return SendResult(success=True)
        if self._closing:
            return SendResult(success=False, error="adapter is disconnected", error_kind="unknown")
        ledger = self.ledger if ledgered else None
        chunk_count = len(chunks)
        obligations = (
            [
                ledger.record(
                    session_key,
                    chat_id,
                    chunk,
                    reply_to=self._chunk_reply_to(reply_to, index),
                    thread_id=thread_id,
                    chunk_index=index,
                    chunk_count=chunk_count,
                )
                for index, chunk in enumerate(chunks)
            ]
            if ledger is not None
            else []
        )
        last = SendResult(success=True)
        for index, chunk in enumerate(chunks):
            if ledger is not None and obligations[index].state in {"delivered", "attempting"}:
                continue
            anchor = self._chunk_reply_to(reply_to, index)
            if ledger is not None:
                ledger.mark_attempting(obligations[index].obligation_id)
            try:
                last = await self._send_chunk_with_retry(
                    chat_id, chunk, reply_to=anchor, thread_id=thread_id
                )
            except asyncio.CancelledError:
                if ledger is not None:
                    ledger.mark_unknown(obligations[index].obligation_id, "delivery task cancelled")
                raise
            if ledger is not None:
                if last.success:
                    ledger.mark_delivered(obligations[index].obligation_id)
                elif last.error_kind in {"timeout", "unknown"} or is_timeout_error(last.error):
                    ledger.mark_unknown(obligations[index].obligation_id, last.error or "unknown")
                else:
                    ledger.mark_failed(
                        obligations[index].obligation_id, last.error or "send failed"
                    )
            if not last.success:
                return last
        return last

    def _chunk_reply_to(self, reply_to: str | None, index: int) -> str | None:
        """Return the reply anchor for a chunk; platforms may prefer first-only anchors."""
        return reply_to

    async def _send_chunk(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        """Send one already-sized chunk; adapters override this to avoid re-splitting."""
        return await self.send(chat_id, content, reply_to=reply_to, thread_id=thread_id)

    async def _send_chunk_with_retry(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        result = SendResult(success=False, error="not attempted")
        for attempt in range(self.send_attempts):
            try:
                result = await self._send_chunk(
                    chat_id, content, reply_to=reply_to, thread_id=thread_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = SendResult(success=False, error=str(exc), error_kind="unknown")
            if result.success:
                return result
            logger.warning(
                "[%s] send to %s failed (attempt %d/%d): %s",
                self.name,
                chat_id,
                attempt + 1,
                self.send_attempts,
                result.error,
            )
            if attempt + 1 < self.send_attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
        return result

    async def _quiet(self, call: Awaitable[None], label: str) -> None:
        try:
            await call
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[%s] %s failed", self.name, label, exc_info=True)

    async def send_with_retry(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        result = SendResult(success=False, error="not attempted")
        for attempt in range(self.send_attempts):
            try:
                result = await self.send(chat_id, content, reply_to=reply_to, thread_id=thread_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = SendResult(success=False, error=str(exc), error_kind="unknown")
            if result.success:
                return result
            logger.warning(
                "[%s] send to %s failed (attempt %d/%d): %s",
                self.name,
                chat_id,
                attempt + 1,
                self.send_attempts,
                result.error,
            )
            if attempt + 1 < self.send_attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
        return result

    async def cancel_background_tasks(self) -> None:
        self._closing = True
        tasks = [*self._active.values(), *self._direct]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10.0)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                logger.error(
                    "[%s] %d task(s) ignored cancellation during disconnect",
                    self.name,
                    len(pending),
                )
        self._active.clear()
        self._direct.clear()

    async def wait_idle(self) -> None:
        """Wait for every in-flight turn, including follow-ups it schedules."""
        while self._active or self._direct:
            tasks = [*self._active.values(), *self._direct]
            await asyncio.gather(*tasks, return_exceptions=True)


def split_message(text: str, limit: int) -> list[str]:
    """Split a reply into chunks a platform accepts, preferring paragraph boundaries.

    Chunks are cut at blank lines when possible, then at line breaks, and only as a last
    resort inside a line. Code fences that get split are closed and reopened so every
    chunk renders on its own.
    """
    if limit < 1:
        raise ValueError("Message limit must be positive")
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return _balance_fences([chunk for chunk in chunks if chunk])


def _balance_fences(chunks: list[str]) -> list[str]:
    balanced: list[str] = []
    open_fence: str | None = None
    for chunk in chunks:
        prefix = f"{open_fence}\n" if open_fence is not None else ""
        for line in chunk.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                open_fence = None if open_fence is not None else stripped
        suffix = "\n```" if open_fence is not None else ""
        balanced.append(f"{prefix}{chunk}{suffix}")
    return balanced


__all__ = [
    "BYPASS_WHEN_BUSY",
    "ApprovalHandler",
    "RETRYABLE_ERROR_PATTERNS",
    "BasePlatformAdapter",
    "MessageEvent",
    "MessageHandler",
    "MessageType",
    "SendErrorKind",
    "SendResult",
    "StreamHandle",
    "is_retryable_error",
    "is_timeout_error",
    "split_message",
]
