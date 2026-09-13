"""One scrolling terminal frontend with streaming output and an interruptible editor."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, is_done, to_filter
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import DummyHistory, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import Output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

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
from run_agent_core.types import JSONValue

ACCENT = "#d79a72"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_MARKDOWN = re.compile(r"(?m)^(?:#{1,6} |```|~~~|[-*] |\d+\. )|\*\*.+?\*\*")


def _safe_text(value: str) -> str:
    return _CONTROL.sub("", value)


def _short(value: str, width: int) -> str:
    text = Text(" ".join(_safe_text(value).split()))
    text.truncate(max(1, width), overflow="ellipsis")
    return text.plain


def _tool_summary(args: dict[str, JSONValue], width: int) -> str:
    # Show a useful target, never dump arbitrary payloads or credential fields.
    for key in ("description", "command", "cmd", "path", "file_path", "pattern", "query", "url"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return _short(value, width)
    return f"{len(args)} arguments" if args else ""


class _CommandCompleter(Completer):
    def __init__(self, application: CodingApplication) -> None:
        self.application = application

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        prefix = document.text_before_cursor
        if not prefix.startswith("/") or any(character.isspace() for character in prefix):
            return
        session = self.application.session
        commands = {
            f"/{command.name}": command.description
            for command in session.command_registry.list_commands()
        }
        for command in session.command_registry.list_commands():
            commands.update({f"/{alias}": command.description for alias in command.aliases})
        commands.update(
            {f"/skill:{skill.name}": skill.description or "Skill" for skill in session.skills}
        )
        commands.update(
            {
                f"/{item.name}": item.description or "Prompt template"
                for item in session.prompt_templates
            }
        )
        commands.update(
            {
                "/stop": "Stop the current operation.",
                "/queue": "Queue a follow-up after the current task.",
                "/expand": "List tool results or show one: /expand <id>.",
            }
        )
        for name, description in sorted(commands.items()):
            if name.startswith(prefix.lower()):
                yield Completion(
                    name, start_position=-len(prefix), display_meta=_short(description, 72)
                )


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
            self.status[(source, key)] = " ".join(_safe_text(text).splitlines())

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
        self.console.print(Text(_safe_text(message), style="red" if level == "error" else "dim"))

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
        self.console.print(Text(_safe_text(title)))
        for index, label in enumerate(options, 1):
            self.console.print(Text(f"  {index}. {_safe_text(label)}"))
        answer = await self.input("Select number (blank cancels)", timeout=timeout)
        if answer and answer.strip().isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
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
        self._tool_names: dict[str, str] = {}
        self._tool_started: dict[str, float] = {}
        self._started_at: float | None = None
        self._activity = "Ready"
        self._tool_count = 0
        self._output_tokens = 0
        self._context_tokens = application.session.context_token_estimate
        self._response_text = ""
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
            bottom_toolbar=self._toolbar,
            history=history,
            key_bindings=bindings,
            multiline=True,
            completer=_CommandCompleter(application),
            complete_while_typing=True,
            reserve_space_for_menu=5,
            show_frame=True,
            refresh_interval=0.5,
            prompt_continuation=[("class:prompt", "· ")],
            placeholder=lambda: [
                (
                    "class:placeholder",
                    "Add a correction, or /queue a follow-up…"
                    if self.application.session.is_running
                    else "Ask a question or describe a coding task…",
                )
            ],
            style=Style.from_dict(
                {
                    "prompt": f"bold {ACCENT}",
                    "frame.border": ACCENT,
                    "placeholder": "#808080",
                    "bottom-toolbar": "noreverse",
                    "status": ACCENT,
                    "hint": "#808080",
                    "completion-menu": "bg:#262626 #d0d0d0",
                    "completion-menu.completion.current": "bg:#d79a72 #1c1c1c",
                    "completion-menu.meta.completion": "bg:#262626 #a0a0a0",
                    "completion-menu.meta.completion.current": "bg:#3a302b #e0c5b0",
                }
            ),
        )
        self.dialog_editor: PromptSession[str] = PromptSession(
            input=terminal_input, output=terminal_output, history=DummyHistory()
        )
        self.editor.layout.current_window.dont_extend_height = to_filter(True)
        # Like Tau's active transcript widget, the unfinished response belongs to
        # the editor renderer. Flushing partial stdout lines lets redraw erase them.
        self.editor.layout.container = HSplit(
            [
                ConditionalContainer(
                    Window(
                        FormattedTextControl(
                            self._response_fragments,
                            get_cursor_position=self._response_cursor,
                        ),
                        height=Dimension(max=8),
                        dont_extend_height=True,
                        wrap_lines=True,
                        always_hide_cursor=True,
                    ),
                    filter=Condition(lambda: bool(self._response_text)) & ~is_done,
                ),
                self.editor.layout.container,
            ]
        )

    def _response_fragments(self) -> StyleAndTextTuples:
        return [("class:prompt", "● Run Agent\n"), ("", self._response_text)]

    def _response_cursor(self) -> Point:
        lines = self._response_text.split("\n")
        return Point(x=len(lines[-1]), y=len(lines))

    def _finish_response(self, text: str | None = None) -> None:
        text = self._response_text if text is None else _safe_text(text)
        self._response_text = ""
        self.editor.app.invalidate()
        if text:
            self.console.print(Text("\n● Run Agent", style=f"bold {ACCENT}"))
            if _MARKDOWN.search(text):
                self.console.print(Markdown(text))
            else:
                self.console.print(Text(text), soft_wrap=True)

    def _toolbar(self) -> StyleAndTextTuples:
        session = self.application.session
        width = max(1, self.editor.output.get_size().columns - 2)
        activity = self._activity
        if self._started_at is not None:
            activity += f" · {monotonic() - self._started_at:.0f}s"
        window = session.context_window_tokens
        context = f"ctx ~{self._context_tokens / window:.0%}" if window > 0 else "ctx n/a"
        state = f" {activity} · {session.model} · {context}"
        if self.ui.status:
            state += " · " + " · ".join(self.ui.status.values())
        hint = (
            " Enter corrects · /queue next task · Ctrl+C stop"
            if session.is_running
            else " Enter send · Alt+Enter newline · / commands · Ctrl+D exit"
        )
        return [
            ("class:status", _short(state, width)),
            ("", "\n"),
            ("class:hint", _short(hint, width)),
        ]

    def _welcome(self) -> None:
        session = self.application.session
        content = Text()
        content.append("Your coding workspace\n", style="bold")
        content.append(f"\n{_safe_text(str(session.cwd))}\n")
        content.append(
            f"{_safe_text(session.provider_name)} / {_safe_text(session.model)}\n", style=ACCENT
        )
        content.append(f"Session {_safe_text(session.session_id or 'new')}\n", style="dim")
        content.append("\n/help commands   /resume history   /model switch model", style="dim")
        self.console.print()
        self.console.print(
            Panel(
                content,
                title=Text(" Run Agent ", style=f"bold {ACCENT}"),
                title_align="left",
                border_style=ACCENT,
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        self.console.print()

    async def _prompt(self, label: str) -> str:
        try:
            prefill, self._prefill = self._prefill, ""
            return await self.editor.prompt_async(
                [("class:prompt", label)], default=prefill, show_frame=True
            )
        except KeyboardInterrupt as exc:
            raise _InputInterrupted from exc

    async def _dialog_prompt(self, dialog: _Dialog) -> str:
        try:
            return await self.dialog_editor.prompt_async(
                f"{_safe_text(dialog.title)}\n❯ ", is_password=dialog.secret
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
            self._response_text += _safe_text(event.assistant_message_event.delta)
            self._activity = "Responding"
            self.editor.app.invalidate()
        elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
            # The final message is authoritative, including text absent from deltas.
            self._finish_response(event.message.text or None)
            self._output_tokens += event.message.usage.output
            self._context_tokens = self.application.session.context_token_estimate
            self._activity = "Working"
        elif isinstance(event, ToolExecutionStartEvent):
            self._finish_response()
            self._tool_count += 1
            self._tool_started[event.tool_call_id] = monotonic()
            self._tool_names[event.tool_call_id] = event.tool_name
            self._activity = f"Running {event.tool_name}"
            name = _short(event.tool_name, max(8, self.console.width // 3))
            summary = _tool_summary(event.args, self.console.width - len(name) - 8)
            line = Text()
            line.append(f"\n  › {name}", style=f"bold {ACCENT}")
            if summary:
                line.append(f"  {summary}", style="default")
            self.console.print(line)
        elif isinstance(event, ToolExecutionEndEvent):
            output = _safe_text(
                "\n".join(getattr(block, "text", "") for block in event.result.content)
            )
            self._tools[event.tool_call_id] = output
            lines = output.splitlines()
            style = "red" if event.is_error else "dim"
            for output_line in lines[:4]:
                preview = Text(output_line.expandtabs(4))
                preview.truncate(max(1, self.console.width - 6), overflow="ellipsis")
                self.console.print(Text("    │ ", style="dim") + Text(preview.plain, style=style))
            started = self._tool_started.pop(event.tool_call_id, None)
            timing = f" · {monotonic() - started:.1f}s" if started is not None else ""
            status = "Error" if event.is_error else "Done"
            count = f" · {len(lines)} lines" if lines else " · no text output"
            self.console.print(Text(f"    {status}{timing}{count}", style=style))
            self.console.print(Text(f"    /expand {_safe_text(event.tool_call_id)}", style="dim"))
            self._activity = (
                f"Running {len(self._tool_started)} tools" if self._tool_started else "Working"
            )
        elif isinstance(event, AgentSettledEvent):
            self._finish_response()
            elapsed = (
                f" · {monotonic() - self._started_at:.1f}s" if self._started_at is not None else ""
            )
            tokens = f" · {self._output_tokens:,} output tokens" if self._output_tokens else ""
            self.console.print()
            self.console.print(
                Text(
                    f"  {event.status}{elapsed} · {self._tool_count} tool calls{tokens}",
                    style="green" if event.status == "succeeded" else "yellow",
                )
            )

    async def _consume(self, text: str) -> None:
        self._started_at = monotonic()
        self._activity = "Working"
        self._tool_count = 0
        self._output_tokens = 0
        try:
            if text.startswith("/"):
                result = await self.application.command(text)
                if result.handled:
                    if result.message:
                        self.console.print(Text(_safe_text(result.message)))
                    if text.strip() == "/help":
                        self.console.print(
                            Text(
                                "\nTerminal commands:\n"
                                "/stop                 Stop the current operation.\n"
                                "/queue <text>         Queue a follow-up task.\n"
                                "/expand [tool-id]     List tool results or show full output.",
                                style="dim",
                            )
                        )
                    self._exit = result.exit_requested
                    if self._exit:
                        self._shutdown.set()
                    return
            async for event in self.application.prompt(text):
                self.render(event)
        except asyncio.CancelledError:
            self._finish_response()
            self.console.print(Text("\n  Stopped. Ready for your next request.", style="yellow"))
        except Exception as exc:
            self._finish_response()
            self.console.print(Text(f"\n{type(exc).__name__}: {_safe_text(str(exc))}", style="red"))
        finally:
            self._finish_response()
            self._started_at = None
            self._activity = "Ready"
            self._tool_started.clear()
            self._context_tokens = self.application.session.context_token_estimate

    def _expand(self, key: str) -> None:
        if key:
            self.console.print(
                Text(self._tools.get(key, f"Unknown tool result: {_safe_text(key)}. Use /expand."))
            )
        elif self._tools:
            self.console.print(Text("Tool results · /expand <id> for full output", style=ACCENT))
            for identity, output in self._tools.items():
                name = self._tool_names.get(identity, "tool")
                self.console.print(
                    Text(
                        f"  {_safe_text(identity)}  {_safe_text(name)}  "
                        f"{len(output.splitlines())} lines"
                    )
                )
        else:
            self.console.print(Text("No tool output yet.", style="dim"))

    async def _stop(self) -> None:
        self.application.session.cancel()
        if self._work is not None and not self._work.done():
            self._work.cancel()
            await asyncio.gather(self._work, return_exceptions=True)

    async def run(self, initial_prompt: str = "") -> None:
        self._welcome()
        self._activity = "Starting"
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
                    self._activity = "Ready"
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
                    if text == "/expand" or text.startswith("/expand "):
                        key = text.partition(" ")[2].strip()
                        self._expand(key)
                        continue
                    if self._work is not None and not self._work.done():
                        if self.application.session.is_running and not text.startswith("/"):
                            self.application.session.queue_steering_message(text)
                            self.ui.notify("Correction queued for the next tool boundary.")
                        elif text.startswith("/queue "):
                            self.application.session.queue_follow_up_message(text[7:])
                            self.ui.notify("Follow-up queued after the current task.")
                        elif text in {"/help", "/hotkeys", "/session"}:
                            result = await self.application.command(text)
                            if result.message:
                                self.console.print(Text(_safe_text(result.message)))
                        else:
                            self.ui.notify("Current operation is active; Ctrl+C stops it.")
                        continue
                    if self._work is not None:
                        self._work.result()
                    self._work = asyncio.create_task(self._consume(text))
            finally:
                await self._stop()
