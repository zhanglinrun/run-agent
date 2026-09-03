"""Tiny asynchronous event bus used by AgentCore and Harness observers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


EventListener = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    async def emit(self, name: str, **payload: Any) -> None:
        for listener in tuple(self._listeners):
            result = listener(name, dict(payload))
            if asyncio.iscoroutine(result):
                await result
