"""One scrolling terminal frontend with streaming output and an interruptible editor."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import DummyHistory, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent, CodingSessionEvent
from run_agent_coding.extensions.api import NotifyLevel, NullUiBridge
from run_agent_core.events import (
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from run_agent_core.messages import AssistantMessage, UserMessage
from run_agent_core.provider_events import TextDeltaEvent


@dataclass(slots=True)
class _Dialog:
    title: str
    result: asyncio.Future[str | None]
    secret: bool = False
    retired: asyncio.Event | None = None


class _InputInterrupted(Exception):
    pass


class TerminalUi(NullUiBridge):
    def __init__(self, console: Console) -> None:
        self.console = console
        self.dialogs: asyncio.Queue[_Dialog] = asyncio.Queue()
        self.status: dict[tuple[str, str], str] = {}

    def set_status(self, source: str, key: str, text: str | None) -> None:
        if text is None:
            self.status.pop((source, key), None)
        else:
            self.status[(source, key)] = " ".join(text.splitlines())

    def clear_status(self, source: str | None = None) -> None:
        self.status = {
            key: value
            for key, value in self.status.items()
            if source is not None and key[0] != source
        }

    @property
    def has_ui(self) -> bool:
        return True

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        self.console.print(
            message, markup=False, highlight=False, style="red" if level == "error" else "dim"
        )

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        retired = asyncio.Event()
        await self.dialogs.put(_Dialog(title, future, secret, retired))
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            # The reader must have let go of the terminal before the caller runs again,
            # otherwise a line arriving now is consumed by the reader on its way out.
            await asyncio.shield(retired.wait())
            return None

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        answer = await self.input(f"{title}\n{message}\nConfirm [y/N]", timeout=timeout)
        return answer is not None and answer.lower().strip() in {"y", "yes"}

    async def select(
        self, title: str, options: Sequence[str], *, timeout: float | None = None
    ) -> str | None:
        self.console.print(title, markup=False)
        for index, label in enumerate(options, 1):
            self.console.print(f"  {index}. {label}", markup=False)
        answer = await self.input("Select number (blank cancels)", timeout=timeout)
        if answer and answer.strip().isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return None


class DirectTerminalUi(TerminalUi):
    """Credential setup before a model session exists."""

    def __init__(self) -> None:
        super().__init__(Console(highlight=False))
        self.editor: PromptSession[str] = PromptSession(history=DummyHistory())

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        try:
            async with asyncio.timeout(timeout):
                return await self.editor.prompt_async(f"{title}: ", is_password=secret)
        except (KeyboardInterrupt, EOFError, TimeoutError):
            return None


class Terminal:
    def __init__(
        self,
        application: CodingApplication,
        *,
        console: Console | None = None,
        terminal_input: Input | None = None,
        terminal_output: Output | None = None,
    ) -> None:
        self.application = application
        self.console = console or Console(highlight=False)
        self.ui = TerminalUi(self.console)
        self._work: asyncio.Task[None] | None = None
        self._tools: dict[str, str] = {}
        self._streamed = False
        self._exit = False
        self._shutdown = asyncio.Event()
        self._prefill = ""
        history = InMemoryHistory()
        for message in application.session.messages:
            if isinstance(message, UserMessage):
                history.append_string(message.text)
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event: KeyPressEvent) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        self.editor: PromptSession[str] = PromptSession(
            input=terminal_input,
            output=terminal_output,
            bottom_toolbar=lambda: " | ".join(self.ui.status.values()),
            history=history,
            key_bindings=bindings,
            multiline=True,
            completer=WordCompleter(
                [
                    "/help",
                    "/new",
                    "/resume",
                    "/model",
                    "/thinking",
                    "/tree",
                    "/compact",
                    "/reload",
                    "/name",
                    "/export",
                    "/stop",
                    "/queue",
                    "/expand",
                    "/quit",
                ]
            ),
        )
        self.dialog_editor: PromptSession[str] = PromptSession(
            input=terminal_input, output=terminal_output, history=DummyHistory()
        )

    async def _prompt(self, label: str) -> str:
        try:
            prefill, self._prefill = self._prefill, ""
            return await self.editor.prompt_async(label, default=prefill)
        except KeyboardInterrupt as exc:
            raise _InputInterrupted from exc

    async def _dialog_prompt(self, dialog: _Dialog) -> str:
        try:
            return await self.dialog_editor.prompt_async(
                f"{dialog.title}\n❯ ", is_password=dialog.secret
            )
        except KeyboardInterrupt as exc:
            raise _InputInterrupted from exc

    async def _read(self) -> str:
        """One editor owns the terminal; extension dialogs suspend normal input."""
        while True:
            typing = asyncio.create_task(self._prompt("❯ "))
            dialog_ready = asyncio.create_task(self.ui.dialogs.get())
            shutdown = asyncio.create_task(self._shutdown.wait())
            try:
                done, _ = await asyncio.wait(
                    [typing, dialog_ready, shutdown], return_when=asyncio.FIRST_COMPLETED
                )
                if shutdown in done:
                    raise EOFError
                if dialog_ready in done:
                    saved = self.editor.default_buffer.text
                    typing.cancel()
                    with suppress(asyncio.CancelledError, _InputInterrupted, EOFError):
                        await typing
                    dialog = dialog_ready.result()
                    if dialog.result.done():
                        self._prefill = saved
                        if dialog.retired is not None:
                            dialog.retired.set()
                        continue
                    answering = asyncio.create_task(self._dialog_prompt(dialog))
                    try:

                        async def dialog_done(request: _Dialog = dialog) -> None:
                            with suppress(asyncio.CancelledError):
                                await asyncio.shield(request.result)

                        timeout_waiter = asyncio.create_task(dialog_done())
                        done, _ = await asyncio.wait(
                            [answering, timeout_waiter, shutdown],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if shutdown in done:
                            raise EOFError
                        if answering in done and not dialog.result.done():
                            try:
                                answer = answering.result()
                            except (_InputInterrupted, EOFError):
                                answer = None
                            dialog.result.set_result(answer)
                    finally:
                        answering.cancel()
                        timeout_waiter.cancel()
                        await asyncio.gather(answering, timeout_waiter, return_exceptions=True)
                        if dialog.retired is not None:
                            dialog.retired.set()
                    self._prefill = saved
                else:
                    return typing.result()
            finally:
                typing.cancel()
                dialog_ready.cancel()
                shutdown.cancel()
                await asyncio.gather(typing, dialog_ready, shutdown, return_exceptions=True)

    def render(self, event: CodingSessionEvent) -> None:
        if isinstance(event, MessageUpdateEvent) and isinstance(
            event.assistant_message_event, TextDeltaEvent
        ):
            self.console.print(event.assistant_message_event.delta, end="", markup=False)
            self._streamed = True
        elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
            if self._streamed:
                self.console.print()
            elif event.message.text:
                self.console.print(event.message.text, markup=False)
            self._streamed = False
        elif isinstance(event, ToolExecutionStartEvent):
            self.console.print(f"  → {event.tool_name}  {event.args}", style="dim", markup=False)
        elif isinstance(event, ToolExecutionEndEvent):
            output = "\n".join(getattr(block, "text", "") for block in event.result.content)
            self._tools[event.tool_call_id] = output
            lines = output.splitlines()
            self.console.print(
                "\n".join(lines[:6]), style="red" if event.is_error else "dim", markup=False
            )
            if len(lines) > 6:
                self.console.print(
                    f"  … {len(lines) - 6} more lines. /expand {event.tool_call_id}", style="dim"
                )
        elif isinstance(event, AgentSettledEvent):
            self.console.print(f"  {event.status} · {event.run_id[:12]}", style="dim", markup=False)

    async def _consume(self, text: str) -> None:
        try:
            if text.startswith("/"):
                result = await self.application.command(text)
                if result.handled:
                    if result.message:
                        self.console.print(result.message, markup=False)
                    self._exit = result.exit_requested
                    if self._exit:
                        self._shutdown.set()
                    return
            async for event in self.application.prompt(text):
                self.render(event)
        except asyncio.CancelledError:
            self.console.print("Stopped.", style="dim")
        except Exception as exc:
            self.console.print(f"{type(exc).__name__}: {exc}", style="red", markup=False)

    async def _stop(self) -> None:
        self.application.session.cancel()
        if self._work is not None and not self._work.done():
            self._work.cancel()
            await asyncio.gather(self._work, return_exceptions=True)

    async def run(self, initial_prompt: str = "") -> None:
        self.console.print("Run Agent", style="bold")
        self.console.print(
            f"{self.application.session.cwd} · {self.application.session.session_id}",
            style="dim",
            markup=False,
        )
        self.console.print(
            f"{self.application.session.provider_name}:{self.application.session.model}",
            style="dim",
            markup=False,
        )
        self.console.print(
            "Enter sends · Alt+Enter adds a line · Ctrl+C stops · /help", style="dim"
        )
        with (
            create_app_session(input=self.editor.input, output=self.editor.output),
            patch_stdout(raw=True),
        ):
            initializing = True
            startup_inputs: list[str] = []

            async def initial() -> None:
                nonlocal initializing
                try:
                    await self.application.start(self.ui)
                    initializing = False
                    if initial_prompt:
                        await self._consume(initial_prompt)
                    for queued in startup_inputs:
                        if not self._exit:
                            await self._consume(queued)
                except Exception as exc:
                    self.ui.notify(str(exc), level="error")
                    self._shutdown.set()
                finally:
                    initializing = False

            self._work = asyncio.create_task(initial())
            try:
                while not self._exit:
                    try:
                        text = (await self._read()).strip()
                    except _InputInterrupted:
                        await self._stop()
                        continue
                    except EOFError:
                        break
                    if not text:
                        continue
                    if initializing and text != "/stop":
                        startup_inputs.append(text)
                        continue
                    if text == "/stop":
                        await self._stop()
                        continue
                    if text.startswith("/expand"):
                        key = text.partition(" ")[2].strip()
                        self.console.print(
                            self._tools.get(key, "\n".join(self._tools) or "No tool output yet."),
                            markup=False,
                        )
                        continue
                    if self._work is not None and not self._work.done():
                        if self.application.session.is_running and not text.startswith("/"):
                            self.application.session.queue_steering_message(text)
                            self.ui.notify("Correction queued for the next tool boundary.")
                        elif text.startswith("/queue "):
                            self.application.session.queue_follow_up_message(text[7:])
                        else:
                            self.ui.notify("Current operation is active; Ctrl+C stops it.")
                        continue
                    if self._work is not None:
                        self._work.result()
                    self._work = asyncio.create_task(self._consume(text))
            finally:
                await self._stop()
