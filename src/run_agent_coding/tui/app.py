"""Tau-inspired full-screen host for the public CodingApplication API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from time import monotonic
from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from run_agent_coding.application import CodingApplication
from run_agent_coding.tui.adapter import TuiEventAdapter
from run_agent_coding.tui.autocomplete import UI_COMMANDS, CompletionItem, get_completions
from run_agent_coding.tui.bridge import TuiBridge
from run_agent_coding.tui.screens import OutputScreen
from run_agent_coding.tui.state import TuiState
from run_agent_coding.tui.themes import RUN_DARK, RUN_HIGH_CONTRAST, RUN_LIGHT
from run_agent_coding.tui.widgets import PromptInput, SessionSidebar, TranscriptView
from run_agent_core.messages import UserMessage


class RunAgentTui(App[None]):
    TITLE = "Run Agent"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #topbar { height: 3; background: $surface; padding: 0 1; }
    #brand { width: 1fr; height: 3; content-align: left middle; color: $primary;
        text-style: bold; }
    #topbar Button { min-width: 9; margin-left: 1; height: 3; }
    #workspace { height: 1fr; }
    #main { width: 1fr; }
    #sidebar { width: 28; }
    #composer { height: auto; padding: 0 2; }
    #queue { height: auto; max-height: 3; color: $warning; }
    #completions { height: auto; max-height: 9; border: round $secondary;
        background: $surface; }
    #status { height: 1; color: $text-muted; margin: 0 0 1 0; }
    Footer { background: $surface; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+p", "commands", "Commands", priority=True),
        Binding("ctrl+b,f2", "sidebar", "Sidebar", priority=True),
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("escape", "stop", "Stop", show=False),
        Binding("ctrl+d", "quit_empty", "Quit", priority=True),
        Binding("ctrl+e", "expand_tools", "Tools", priority=True),
    ]

    def __init__(self, application: CodingApplication, initial_prompt: str = "") -> None:
        super().__init__()
        self.application = application
        self.initial_prompt = initial_prompt
        self.state = TuiState()
        self.adapter = TuiEventAdapter(self.state)
        self.ui = TuiBridge(self)
        self.ready = False
        self._operation: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._sync_lock = asyncio.Lock()
        self._completions: list[CompletionItem] = []
        self._sidebar_visible = True
        self._expanded = False
        for theme in (RUN_DARK, RUN_LIGHT, RUN_HIGH_CONTRAST):
            self.register_theme(theme)
        self.theme = "run-dark"
        self._preferences = application.manager.paths.home / "tui.json"
        try:
            saved = json.loads(self._preferences.read_text(encoding="utf-8"))
            if saved.get("theme") in {"run-dark", "run-light", "run-high-contrast"}:
                self.theme = saved["theme"]
            self._sidebar_visible = bool(saved.get("sidebar", True))
        except (OSError, ValueError, AttributeError):
            pass

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Text(f"◈ Run Agent  /  {self.application.session.cwd.name}"), id="brand")
            yield Button("New", id="new")
            yield Button("Sessions", id="sessions")
            yield Button("Model", id="model")
        with Horizontal(id="workspace"):
            with Vertical(id="main"):
                yield TranscriptView(id="transcript")
                with Vertical(id="composer"):
                    yield Static("", id="queue", markup=False)
                    yield OptionList(id="completions")
                    yield PromptInput(id="prompt")
                    yield Static("Starting…", id="status", markup=False)
            yield SessionSidebar(id="sidebar")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#completions").display = False
        self.query_one(PromptInput).disabled = True
        await self.reload_transcript()
        self.set_interval(0.25, self.refresh_status)
        self._operation = self.spawn(self.initialize())

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def initialize(self) -> None:
        try:
            await self.application.start(self.ui)
            self.ready = True
            self.query_one(PromptInput).disabled = False
            self.focus_prompt()
            if self.initial_prompt:
                await self.consume(self.initial_prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.add_notice(f"Startup failed: {exc}", error=True)

    def focus_prompt(self) -> None:
        if self.is_running and len(self.screen_stack) == 1:
            self.query_one(PromptInput).focus()

    async def reload_transcript(self) -> None:
        self.state.load_messages(self.application.session.messages)
        self.adapter = TuiEventAdapter(self.state)
        self.query_one(PromptInput).set_history(
            [
                message.text
                for message in self.application.session.messages
                if isinstance(message, UserMessage)
            ]
        )
        await self.sync_transcript()

    async def sync_transcript(self) -> None:
        async with self._sync_lock:
            await self.query_one(TranscriptView).sync_items(tuple(self.state.items))
        self.refresh_status()

    def add_notice(self, text: str, *, error: bool = False) -> None:
        self.state.add("error" if error else "notice", text, is_error=error)
        if self.is_running:
            self.spawn(self.sync_transcript())

    def refresh_status(self) -> None:
        if not self.query("#status"):
            return
        session = self.application.session
        used, window = session.context_token_estimate, session.context_window_tokens
        context = f"~{used:,} / {window:,}" if window else f"~{used:,}"
        elapsed = (
            f" · {monotonic() - self.state.started_at:.1f}s"
            if self.state.running and self.state.started_at is not None
            else ""
        )
        self.query_one("#status", Static).update(
            Text(
                f"{self.state.activity}  ·  {session.model}  ·  {session.thinking_level}"
                f"  ·  ctx {context}  ·  {self.state.tool_count} tools{elapsed}"
            )
        )
        queues = []
        if self.state.queued_steering:
            queues.append(f"Corrections queued: {len(self.state.queued_steering)}")
        if self.state.queued_follow_up:
            queues.append(f"Follow-ups queued: {len(self.state.queued_follow_up)}")
        self.query_one("#queue", Static).update(Text(" · ".join(queues)))
        sidebar = self.query_one(SessionSidebar)
        sidebar.display = self._sidebar_visible and self.size.width >= 100
        sidebar.update_info(
            f"{session.session_title or 'Untitled session'}\n{session.session_id}\n\n"
            f"WORKSPACE\n{session.cwd}\n\nMODEL\n{session.provider_name}\n{session.model}\n"
            f"Thinking: {session.thinking_level}\n\nCONTEXT\n{context} tokens\n\n"
            f"TOOLS & SKILLS\n{len(session.tools)} tools · {len(session.skills)} skills\n\n"
            f"RUN\n{self.state.activity}\n{self.state.output_tokens:,} output tokens\n\n"
            + "\n".join(self.ui.status.values())
        )

    def on_resize(self, event: events.Resize) -> None:
        self.refresh_status()

    @on(TextArea.Changed, "#prompt")
    def update_completions(self) -> None:
        prompt = self.query_one(PromptInput)
        self._completions = get_completions(self.application.session, prompt.text)
        exact = len(self._completions) == 1 and self._completions[0].text == prompt.text
        prompt.completion_active = bool(self._completions) and not exact
        listing = self.query_one("#completions", OptionList)
        listing.clear_options()
        listing.add_options(
            [Option(Text(f"{item.text:18} {item.description}")) for item in self._completions]
        )
        listing.display = prompt.completion_active
        listing.highlighted = 0 if self._completions else None

    def on_prompt_input_completion_moved(self, event: PromptInput.CompletionMoved) -> None:
        listing = self.query_one("#completions", OptionList)
        if self._completions:
            listing.highlighted = ((listing.highlighted or 0) + event.direction) % len(
                self._completions
            )

    def on_prompt_input_completion_accepted(self) -> None:
        self.accept_completion()

    @on(OptionList.OptionSelected, "#completions")
    def accept_completion(self) -> None:
        index = self.query_one("#completions", OptionList).highlighted
        if index is not None and index < len(self._completions):
            prompt = self.query_one(PromptInput)
            prompt.load_text(self._completions[index].text + " ")
            prompt.move_cursor(prompt.document.end)
            prompt.focus()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self.submit(event.text)

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text or not self.ready:
            return
        if text in {"/quit", "/exit"}:
            self.spawn(self.shutdown())
        elif text == "/stop":
            self.action_stop()
        elif self._operation is not None and not self._operation.done():
            if not text.startswith("/") and self.application.session.is_running:
                self.application.session.queue_steering_message(text)
                self.add_notice("Correction queued for the next tool boundary.")
            elif text.startswith("/queue ") and self.application.session.is_running:
                self.application.session.queue_follow_up_message(text[7:].strip())
                self.add_notice("Follow-up queued after the current task.")
            elif text in {"/help", "/hotkeys", "/session"}:
                self.spawn(self.consume(text))
            else:
                self.add_notice("A task is active. Stop it before changing the session.")
        else:
            self._operation = self.spawn(self.consume(text))

    async def consume(self, text: str) -> None:
        try:
            command = text.split()[0]
            if command == "/sidebar":
                self.action_sidebar()
                return
            if command == "/theme":
                selected = await self.ui.select(
                    "Theme", ["run-dark", "run-light", "run-high-contrast"]
                )
                if selected:
                    self.theme = selected
                    self.save_preferences()
                return
            if command == "/clear":
                self.state.clear()
                self.adapter = TuiEventAdapter(self.state)
                await self.sync_transcript()
                return
            if command == "/expand":
                await self.expand(text.partition(" ")[2].strip())
                return
            if text.startswith("/"):
                result = await self.application.command(text)
                if result.handled:
                    if command in {"/new", "/resume", "/tree", "/branch", "/rewind", "/fork"}:
                        await self.reload_transcript()
                    if result.message:
                        if command in {
                            "/help",
                            "/hotkeys",
                            "/session",
                            "/tools",
                            "/skills",
                            "/prompts",
                        }:
                            message = result.message
                            if command == "/help":
                                message += "\n\nTUI commands\n" + "\n".join(
                                    f"{name}: {description}"
                                    for name, description in UI_COMMANDS.items()
                                )
                            await self.ui.dialog(OutputScreen(command, message))
                        else:
                            self.add_notice(result.message)
                    if result.exit_requested:
                        self.exit()
                    return
            async for event in self.application.prompt(text):
                changed = self.adapter.apply(event)
                if changed:
                    await self.sync_transcript()
                else:
                    self.refresh_status()
        except asyncio.CancelledError:
            self.state.running = False
            self.state.activity = "Cancelled"
            self.state.finish_pending()
            await self.sync_transcript()
            raise
        except Exception as exc:
            self.state.running = False
            self.state.activity = "Failed"
            self.state.finish_pending()
            self.add_notice(f"{type(exc).__name__}: {exc}", error=True)
        finally:
            self.refresh_status()

    async def expand(self, key: str) -> None:
        tools = [item for item in self.state.items if item.role == "tool"]
        if not tools:
            self.add_notice("No tool output yet.")
            return
        labels = [f"{item.tool_call_id or item.id}  {item.tool_name}" for item in tools]
        selected = (
            next((item for item in tools if key in {item.id, item.tool_call_id}), None)
            if key
            else None
        )
        if not key:
            label = await self.ui.select("Full tool output", labels)
            selected = tools[labels.index(label)] if label else None
        if selected:
            await self.ui.dialog(
                OutputScreen(selected.tool_name, selected.result_text or selected.text)
            )
        elif key:
            self.add_notice(f"Unknown tool result: {key}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        command = {"new": "/new", "sessions": "/resume", "model": "/model"}.get(
            event.button.id or ""
        )
        if command:
            self.submit(command)

    async def command_picker(self) -> None:
        options = get_completions(self.application.session, "/")
        labels = [f"{item.text}  —  {item.description}" for item in options]
        selected = await self.ui.select("Commands", labels)
        if selected:
            prompt = self.query_one(PromptInput)
            prompt.load_text(options[labels.index(selected)].text + " ")
            prompt.move_cursor(prompt.document.end)

    def action_commands(self) -> None:
        self.spawn(self.command_picker())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if len(self.screen_stack) > 1 and action in {"commands", "sidebar", "expand_tools"}:
            return False
        return super().check_action(action, parameters)

    def save_preferences(self) -> None:
        try:
            self._preferences.parent.mkdir(parents=True, exist_ok=True)
            self._preferences.write_text(
                json.dumps({"theme": self.theme, "sidebar": self._sidebar_visible}),
                encoding="utf-8",
            )
        except OSError as exc:
            self.add_notice(f"Could not save terminal preferences: {exc}")

    def action_sidebar(self) -> None:
        self._sidebar_visible = not self._sidebar_visible
        self.refresh_status()
        self.save_preferences()

    def action_expand_tools(self) -> None:
        self._expanded = not self._expanded
        self.query_one(TranscriptView).set_tools_expanded(self._expanded)

    def action_stop(self) -> None:
        if len(self.screen_stack) > 1:
            return
        self.application.session.cancel()
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()

    def action_quit_empty(self) -> None:
        if len(self.screen_stack) == 1 and not self.query_one(PromptInput).text.strip():
            self.spawn(self.shutdown())

    async def shutdown(self) -> None:
        self.ui.close()
        self.application.session.cancel()
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.exit()

    async def on_unmount(self) -> None:
        self.ui.close()
        self.application.session.cancel()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run_tui_app(application: CodingApplication, initial_prompt: str = "") -> None:
    await RunAgentTui(application, initial_prompt).run_async()
