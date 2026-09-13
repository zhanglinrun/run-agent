"""Async extension dialogs without blocking Textual's message pump."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from textual.screen import ModalScreen

from run_agent_coding.extensions.api import NotifyLevel, NullUiBridge
from run_agent_coding.tui.screens import ConfirmScreen, InputScreen, SelectionScreen

if TYPE_CHECKING:
    from run_agent_coding.tui.app import RunAgentTui

T = TypeVar("T")


class TuiBridge(NullUiBridge):
    def __init__(self, app: RunAgentTui) -> None:
        self.app = app
        self.status: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._pending: set[asyncio.Future[Any]] = set()
        self.closed = False

    @property
    def has_ui(self) -> bool:
        return True

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        self.app.add_notice(message, error=level == "error")

    def set_status(self, source: str, key: str, text: str | None) -> None:
        if text is None:
            self.status.pop((source, key), None)
        else:
            self.status[source, key] = " ".join(text.splitlines())

    def clear_status(self, source: str | None = None) -> None:
        self.status = {
            key: value
            for key, value in self.status.items()
            if source is not None and key[0] != source
        }

    async def dialog(self, screen: ModalScreen[T], timeout: float | None = None) -> T | None:
        try:
            async with asyncio.timeout(timeout), self._lock:
                if self.closed:
                    return None
                future: asyncio.Future[T | None] = asyncio.get_running_loop().create_future()

                def answered(value: T | None) -> None:
                    if not future.done():
                        future.set_result(value)

                # The callback path also works outside a Textual Worker.
                self._pending.add(future)
                try:
                    await self.app.push_screen(screen, answered)
                    return await future
                finally:
                    self._pending.discard(future)
                    if screen in self.app.screen_stack:
                        await screen.dismiss(None)
                    self.app.focus_prompt()
        except TimeoutError:
            return None

    def close(self) -> None:
        self.closed = True
        for future in tuple(self._pending):
            if not future.done():
                future.cancel()

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        return await self.dialog(InputScreen(title, placeholder, secret), timeout)

    async def select(
        self, title: str, options: Sequence[str], *, timeout: float | None = None
    ) -> str | None:
        return await self.dialog(SelectionScreen(title, options), timeout)

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        result = await self.dialog(ConfirmScreen(title, message), timeout)
        return bool(result)
