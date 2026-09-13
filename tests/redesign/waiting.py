"""Poll a condition and report usefully when it never holds."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


async def eventually(check: Callable[[], Awaitable[Any]], *, timeout: float = 5) -> Any:
    """Poll until the condition holds, and say so usefully when it never does.

    A bare TimeoutError leaves a failure uncharacterisable, so a timed-out wait reports
    the elapsed time and the check it was polling.
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            while True:
                result = await check()
                if result:
                    return result
                await asyncio.sleep(0.005)
    except TimeoutError:
        raise AssertionError(
            f"condition stayed false for {time.monotonic() - started:.2f}s "
            f"(budget {timeout}s), still waiting on {getattr(check, '__name__', check)!r}"
        ) from None
