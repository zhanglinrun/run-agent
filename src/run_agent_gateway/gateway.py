"""Pluggable message gateway over the turn scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from run_agent_core.types import JSONValue
from run_agent_gateway.models import TurnLane, TurnRequest, TurnResult
from run_agent_gateway.scheduler import TurnScheduler


@dataclass(frozen=True, slots=True)
class InboundMessage:
    channel: str
    conversation_id: str
    text: str
    id: str = field(default_factory=lambda: uuid4().hex)
    lane: TurnLane = "foreground"
    session_id: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    channel: str
    conversation_id: str
    text: str
    request_id: str
    status: str
    metadata: dict[str, JSONValue] = field(default_factory=dict)


class GatewayAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def messages(self) -> AsyncIterator[InboundMessage]: ...

    async def send(self, message: OutboundMessage) -> None: ...

    async def close(self) -> None: ...


class AgentGateway:
    """Fan in adapter messages and fan out scheduled turn results."""

    def __init__(self, scheduler: TurnScheduler, adapters: Sequence[GatewayAdapter]) -> None:
        names = [adapter.name for adapter in adapters]
        if len(set(names)) != len(names):
            raise ValueError("gateway adapter names must be unique")
        self._scheduler = scheduler
        self._adapters = tuple(adapters)
        self._consumer_tasks: list[asyncio.Task[None]] = []
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._consumer_tasks = [
            asyncio.create_task(
                self._consume(adapter),
                name=f"run-agent-gateway:{adapter.name}",
            )
            for adapter in self._adapters
        ]

    async def shutdown(self, *, grace_period: float = 5.0) -> None:
        if not self._started:
            await self._scheduler.shutdown(grace_period=grace_period)
            return
        await asyncio.gather(*(adapter.close() for adapter in self._adapters))
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        await self._scheduler.shutdown(grace_period=grace_period)
        if self._delivery_tasks:
            await asyncio.gather(*tuple(self._delivery_tasks), return_exceptions=True)
        self._consumer_tasks.clear()
        self._delivery_tasks.clear()
        self._started = False

    async def wait_closed(self) -> None:
        """Wait until every adapter input stream closes."""
        if not self._started:
            return
        await asyncio.gather(*self._consumer_tasks)

    async def _consume(self, adapter: GatewayAdapter) -> None:
        async for message in adapter.messages():
            task = asyncio.create_task(
                self._dispatch(adapter, message),
                name=f"run-agent-gateway-delivery:{adapter.name}:{message.conversation_id}",
            )
            self._delivery_tasks.add(task)
            task.add_done_callback(self._delivery_tasks.discard)

    async def _dispatch(self, adapter: GatewayAdapter, message: InboundMessage) -> None:
        session_id = message.session_id or f"{message.channel}:{message.conversation_id}"
        request = TurnRequest(
            id=message.id,
            session_id=session_id,
            content=message.text,
            lane=message.lane,
            metadata={
                **message.metadata,
                "channel": message.channel,
                "conversation_id": message.conversation_id,
            },
        )
        try:
            result = await self._scheduler.run(request)
        except Exception as exc:  # noqa: BLE001 - scheduler is the gateway host boundary
            result = TurnResult.failed(request, error=str(exc) or type(exc).__name__)
        await adapter.send(_outbound_message(message, result))


def _outbound_message(message: InboundMessage, result: TurnResult) -> OutboundMessage:
    text = result.output
    if result.status != "succeeded" and not text:
        text = result.error or result.status
    return OutboundMessage(
        channel=message.channel,
        conversation_id=message.conversation_id,
        text=text,
        request_id=result.request_id,
        status=result.status,
        metadata=result.metadata,
    )


class QueueGatewayAdapter:
    """In-memory adapter for tests, embedding, and local host integration."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._incoming: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._outgoing: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    async def receive_message(self, message: InboundMessage) -> None:
        if self._closed:
            raise RuntimeError("gateway adapter is closed")
        await self._incoming.put(message)

    async def next_sent(self) -> OutboundMessage:
        return await self._outgoing.get()

    async def send(self, message: OutboundMessage) -> None:
        await self._outgoing.put(message)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._incoming.put(None)

    async def messages(self) -> AsyncIterator[InboundMessage]:
        while True:
            message = await self._incoming.get()
            if message is None:
                return
            yield message


__all__ = [
    "AgentGateway",
    "GatewayAdapter",
    "InboundMessage",
    "OutboundMessage",
    "QueueGatewayAdapter",
]
