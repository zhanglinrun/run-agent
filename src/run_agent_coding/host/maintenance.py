"""Host-owned periodic maintenance callbacks.

Extensions register lifecycle work here without making the host import optional
extension code. A host tick owns when callbacks run; extensions only decide whether
there is work to do.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

MaintenanceCallback = Callable[[float], Awaitable[None]]


class MaintenanceRegistry:
    """Replaceable, named callbacks owned by one host service binding."""

    def __init__(self) -> None:
        self._callbacks: dict[str, MaintenanceCallback] = {}

    def register(self, name: str, callback: MaintenanceCallback) -> Callable[[], None]:
        if not name.strip():
            raise ValueError("maintenance name must not be empty")
        self._callbacks[name] = callback

        def unregister() -> None:
            if self._callbacks.get(name) is callback:
                self._callbacks.pop(name, None)

        return unregister

    async def tick(self, idle_seconds: float = 0.0) -> None:
        for name, callback in tuple(self._callbacks.items()):
            try:
                await callback(idle_seconds)
            except Exception:
                logger.exception("maintenance callback failed: %s", name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._callbacks))


__all__ = ["MaintenanceCallback", "MaintenanceRegistry"]
