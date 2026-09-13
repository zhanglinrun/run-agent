"""Tau-inspired transcript, prompt editor, and session sidebar.

Streaming lifecycle and role-block layout adapted from Tau (MIT); see LICENSE.tau.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Markdown, Static, TextArea
from textual.widgets.markdown import MarkdownStream

if TYPE_CHECKING:
    from run_agent_coding.tui.state import ChatItem


class MessageBlock(Vertical):
    """A stable message node; appending tokens never remounts its siblings."""

    DEFAULT_CSS = """
    MessageBlock { height: auto; margin: 0 0 1 0; padding: 0 1; }
    MessageBlock > .role-label { height: 1; color: $secondary; text-style: bold; }
    MessageBlock > Markdown { margin: 0; padding: 0; height: auto; }
    MessageBlock > Markdown MarkdownParagraph { margin: 0 0 1 0; }
    MessageBlock > Markdown MarkdownFence { overflow-x: auto; }
    MessageBlock.user { background: $surface; border-left: tall $primary; padding: 1; }
    MessageBlock.user > .role-label { color: $primary; }
    MessageBlock.thinking { color: $text-muted; border-left: tall $secondary 45%; }
    MessageBlock.notice { color: $text-muted; }
    MessageBlock.error { border-left: tall $error; color: $error; }
    MessageBlock.tool { padding: 0; }
    MessageBlock Collapsible { padding: 0; background: $surface; border: none; }
    MessageBlock Collapsible > Contents { padding: 0 1 1 2; }
    MessageBlock .tool-arguments { color: $text-muted; height: auto; margin-bottom: 1; }
    MessageBlock .tool-output { height: auto; width: 1fr; }
    """

    def __init__(self, item: ChatItem, *, expanded: bool = False) -> None:
        super().__init__(classes=item.role)
        self.item_id = item.id
        self.role = item.role
        self._text = ""
        self._stream: MarkdownStream | None = None
        self._initial_item = item
        self._expanded = expanded

    def compose(self) -> ComposeResult:
        if self.role == "tool":
            yield Collapsible(
                Static("", markup=False, classes="tool-arguments"),
                Static("", markup=False, classes="tool-output"),
                title="Tool",
                collapsed=not self._expanded,
            )
        else:
            labels = {
                "user": "YOU",
                "assistant": "RUN AGENT",
                "thinking": "THINKING",
                "notice": "INFO",
                "error": "ERROR",
            }
            yield Static(labels.get(self.role, self.role.upper()), classes="role-label")
            if self.role in {"assistant", "thinking"}:
                yield Markdown("", open_links=False)
            else:
                yield Static("", markup=False, classes="message-text")

    async def update_item(self, item: ChatItem) -> None:
        if self.role == "tool":
            status = "Running" if item.pending else "Failed" if item.is_error else "Done"
            target = next(
                (
                    str(item.arguments[key])
                    for key in ("path", "file_path", "command", "cmd", "query", "url")
                    if item.arguments.get(key)
                ),
                "",
            )
            target = " ".join(target.split())
            if len(target) > 90:
                target = target[:87] + "…"
            elapsed = ""
            if item.started_at is not None and item.ended_at is not None:
                elapsed = f" · {max(0, item.ended_at - item.started_at):.2f}s"
            title = f"{status} · {item.tool_name or 'tool'}"
            if target:
                title += f" · {target}"
            # Collapsible titles parse Rich markup; escape external tool arguments.
            from rich.markup import escape

            self.query_one(Collapsible).title = escape(title + elapsed)
            arguments = json.dumps(item.arguments, ensure_ascii=False, indent=2)
            self.query_one(".tool-arguments", Static).update(Text(arguments))
            output = (
                item.result_text or item.text or ("Waiting for output…" if item.pending else "")
            )
            self.query_one(".tool-output", Static).update(Text(output))
            self.set_class(item.is_error, "error")
            return
        if self.role not in {"assistant", "thinking"}:
            if self._text != item.text:
                self.query_one(".message-text", Static).update(Text(item.text))
                self._text = item.text
            return
        body = self.query_one(Markdown)
        if item.pending and item.text.startswith(self._text):
            delta = item.text[len(self._text) :]
            if delta:
                if self._stream is None:
                    self._stream = Markdown.get_stream(body)
                await self._stream.write(delta)
        elif item.text != self._text:
            await self._stop_stream()
            await body.update(item.text)
        if not item.pending:
            await self._stop_stream()
        self._text = item.text
        self.set_class(item.pending, "streaming")

    async def _stop_stream(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            await stream.stop()

    async def on_unmount(self) -> None:
        await self._stop_stream()


class TranscriptView(VerticalScroll):
    """Complete, selectable conversation with explicit user-controlled following."""

    DEFAULT_CSS = """
    TranscriptView { width: 1fr; height: 1fr; padding: 1 2; scrollbar-size: 1 1; }
    TranscriptView > #transcript-welcome { height: auto; margin: 2 1; padding: 1 2;
        border-left: tall $primary; background: $surface; color: $text; }
    """

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._blocks: dict[str, MessageBlock] = {}
        self._tools_expanded = False

    def compose(self) -> ComposeResult:
        yield Static(
            Text.from_markup(
                "[bold]Run Agent[/bold]\n\n"
                "Your coding workspace, one conversation away.\n"
                "分析代码、修改实现、运行验证，支持随时纠正或停止。\n\n"
                "Describe a task below, or type / to explore commands."
            ),
            id="transcript-welcome",
        )

    async def sync_items(self, items: Sequence[ChatItem]) -> None:
        follow = self.is_vertical_scroll_end
        desired = {item.id for item in items}
        welcome = self.query_one("#transcript-welcome", Static)
        welcome.display = not items
        for item_id in list(self._blocks):
            if item_id not in desired:
                await self._blocks.pop(item_id).remove()
        previous: MessageBlock | None = None
        for item in items:
            block = self._blocks.get(item.id)
            if block is not None and block.role != item.role:
                await block.remove()
                block = None
            if block is None:
                block = MessageBlock(item, expanded=self._tools_expanded)
                self._blocks[item.id] = block
                await self.mount(block, after=previous or welcome)
            else:
                # Restore order when replacing the transcript with a resumed branch.
                self.move_child(block, after=previous or welcome)
            await block.update_item(item)
            previous = block
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False, immediate=True)

    def set_tools_expanded(self, expanded: bool) -> None:
        self._tools_expanded = expanded
        for tool in self.query(Collapsible):
            tool.collapsed = not expanded

    async def scroll_latest(self) -> None:
        self.call_after_refresh(self.scroll_end, animate=False, immediate=True)


class PromptInput(TextArea):
    """Native multiline editing with completion messages and draft-safe history."""

    DEFAULT_CSS = """
    PromptInput { height: auto; min-height: 3; max-height: 9; border: round $primary;
        background: $surface; padding: 0 1; }
    PromptInput:focus { border: round $primary; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "submit", "Send", show=False, priority=True),
        Binding("alt+enter,ctrl+j", "newline", "New line", show=False, priority=True),
        Binding("up", "history_previous", "Previous", show=False, priority=True),
        Binding("down", "history_next", "Next", show=False, priority=True),
        Binding("tab", "complete", "Complete", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class CompletionMoved(Message):
        def __init__(self, direction: int) -> None:
            super().__init__()
            self.direction = direction

    class CompletionAccepted(Message):
        pass

    def __init__(
        self,
        *,
        id: str | None = None,
        placeholder: str = "Describe a coding task…",
        classes: str | None = None,
    ) -> None:
        super().__init__(
            id=id,
            classes=classes,
            placeholder=placeholder,
            highlight_cursor_line=False,
            soft_wrap=True,
        )
        self.completion_active = False
        self._history: list[str] = []
        self._history_index = 0
        self._draft = ""

    def set_history(self, history: Sequence[str]) -> None:
        self._history = list(history)
        self._history_index = len(self._history)

    def add_history(self, text: str) -> None:
        if text.strip() and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_index = len(self._history)
        self._draft = ""

    def action_submit(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionAccepted())
        elif self.text.strip():
            text = self.text
            self.add_history(text)
            self.clear()
            self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionAccepted())
        else:
            self.insert("    ")

    def action_history_previous(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionMoved(-1))
        elif self.cursor_location[0] == 0 and not self.selected_text and self._history:
            if self._history_index == len(self._history):
                self._draft = self.text
            self._history_index = max(0, self._history_index - 1)
            self.load_text(self._history[self._history_index])
            self.move_cursor((0, 0))
        else:
            self.action_cursor_up()

    def action_history_next(self) -> None:
        if self.completion_active:
            self.post_message(self.CompletionMoved(1))
        elif (
            self.cursor_location[0] == self.document.line_count - 1
            and not self.selected_text
            and self._history_index < len(self._history)
        ):
            self._history_index += 1
            self.load_text(
                self._draft
                if self._history_index == len(self._history)
                else self._history[self._history_index]
            )
            self.move_cursor(self.document.end)
        else:
            self.action_cursor_down()


class SessionSidebar(Vertical):
    """Safely rendered session facts, independent of transcript scroll position."""

    DEFAULT_CSS = """
    SessionSidebar { width: 30; padding: 1 2; background: $surface;
        border-left: solid $panel; }
    SessionSidebar > .sidebar-heading { color: $primary; text-style: bold;
        height: 2; }
    SessionSidebar > VerticalScroll { height: 1fr; }
    SessionSidebar .sidebar-info { height: auto; }
    SessionSidebar > .sidebar-brand { height: 2; color: $text-muted; }
    """

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._info = ""

    def compose(self) -> ComposeResult:
        yield Static("SESSION", classes="sidebar-heading")
        with VerticalScroll():
            yield Static(Text(self._info), classes="sidebar-info")
        yield Static("RUN AGENT\nLocal coding harness", classes="sidebar-brand")

    def update_info(self, text: str) -> None:
        self._info = text
        if self.is_mounted:
            self.query_one(".sidebar-info", Static).update(Text(text))
