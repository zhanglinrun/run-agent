"""Resolve short ownership changes even when their caller is cancelled."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


async def settle[T](operation: Coroutine[Any, Any, T]) -> tuple[T, bool]:
    """Return the committed result and cancellation flag; never abandon ownership."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled
