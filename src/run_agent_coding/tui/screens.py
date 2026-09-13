"""Keyboard-first modal dialogs inspired by Tau's MIT-licensed TUI."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Markdown, Static

_DIALOG_CSS = """
    SelectionScreen, InputScreen, ConfirmScreen, OutputScreen {
        align: center middle; background: $background 65%; }
    .dialog { width: 76; max-width: 94%; height: auto; max-height: 88%;
        padding: 1 2; border: round $primary; background: $surface; }
    .dialog-title { height: auto; margin-bottom: 1; color: $primary; text-style: bold; }
    .dialog-help { height: auto; margin-top: 1; color: $text-muted; }
    .dialog Input { margin-bottom: 1; }
    .dialog ListView { height: 12; background: $surface; }
    .dialog ListItem { height: auto; padding: 0 1; }
    .dialog Label { height: auto; }
    .dialog Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    .dialog Button { margin-left: 1; }
    .dialog .dialog-message { height: auto; max-height: 12; overflow-y: auto; }
"""


class SelectionScreen(ModalScreen[str | None]):
    """Search display labels while returning the exact original selected value."""

    DEFAULT_CSS = _DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "previous", "Previous", show=False, priority=True),
        Binding("down", "next", "Next", show=False, priority=True),
        Binding("enter", "choose", "Choose", show=False, priority=True),
    ]

    def __init__(self, title: str, options: Sequence[str]) -> None:
        super().__init__()
        self.title_text = title
        self.options = tuple(options)
        self._visible = list(options)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="dialog-title")
            yield Input(placeholder="Search…", id="selection-search")
            yield ListView(*self._rows(), id="selection-list")
            yield Static("No matching options", id="selection-empty")
            yield Static("↑ ↓ select   Enter confirm   Esc cancel", classes="dialog-help")

    def _rows(self) -> list[ListItem]:
        return [ListItem(Label(Text(option))) for option in self._visible]

    def on_mount(self) -> None:
        self.query_one("#selection-search", Input).focus()
        self.query_one(ListView).index = 0 if self._visible else None
        self.query_one("#selection-empty", Static).display = not self._visible

    async def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.casefold().strip()
        self._visible = [option for option in self.options if query in option.casefold()]
        listing = self.query_one(ListView)
        await listing.clear()
        await listing.extend(self._rows())
        listing.index = 0 if self._visible else None
        self.query_one("#selection-empty", Static).display = not self._visible

    def action_previous(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_next(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_choose(self) -> None:
        index = self.query_one(ListView).index
        # Filtering rebuilds the ListView asynchronously; Enter still selects the
        # first filtered result while its rows are being mounted.
        if index is None:
            index = 0
        if index < len(self._visible):
            self.dismiss(self._visible[index])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_choose()

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[str | None]):
    """A transient single-line input; password fields never enter prompt history."""

    DEFAULT_CSS = _DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", secret: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.secret = secret

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="dialog-title")
            yield Input(placeholder=self.placeholder, password=self.secret, id="input-value")
            yield Static("Enter confirm   Esc cancel", classes="dialog-help")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value
        self.query_one(Input).value = ""
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.query_one(Input).value = ""
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Explicit confirmation, initially focused on the non-destructive choice."""

    DEFAULT_CSS = _DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title_text = title
        self.message_text = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="dialog-title")
            yield Static(Text(self.message_text), classes="dialog-message")
            with Horizontal():
                yield Button("Cancel", id="confirm-no")
                yield Button("Confirm", variant="primary", id="confirm-yes")
            yield Static("Tab switch   Enter confirm   Esc cancel", classes="dialog-help")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class OutputScreen(ModalScreen[None]):
    """Full command output in a selectable, scrollable modal."""

    DEFAULT_CSS = (
        _DIALOG_CSS
        + """
    OutputScreen .dialog { width: 110; height: 88%; }
    OutputScreen #output-scroll { height: 1fr; }
    OutputScreen Markdown { padding: 0; margin: 0; height: auto; }
    OutputScreen #output-text { height: auto; }
    """
    )
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]

    def __init__(self, title: str, text: str, markdown: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.output_text = text
        self.markdown = markdown

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="dialog-title")
            with VerticalScroll(id="output-scroll"):
                if self.markdown:
                    yield Markdown(self.output_text, open_links=False)
                else:
                    yield Static(Text(self.output_text), id="output-text")
            yield Static("PgUp / PgDn scroll   Esc close", classes="dialog-help")

    def on_mount(self) -> None:
        self.query_one("#output-scroll", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)
