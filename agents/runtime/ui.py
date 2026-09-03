"""Terminal UI helpers kept outside the provider-neutral AgentCore runtime."""

from __future__ import annotations

import sys
from typing import TextIO

from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _terminal_text(value: object, stream: TextIO) -> str:
    text = str(value).encode("utf-8", errors="replace").decode("utf-8")
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


class _EncodingSafeWriter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)

    def write(self, text: str) -> int:
        return self._stream.write(_terminal_text(text, self._stream))

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


console = Console(file=_EncodingSafeWriter(sys.stdout), highlight=False)


def _safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _safe_stdout_write(text: object) -> None:
    sys.stdout.write(_safe_text(text))
    sys.stdout.flush()


BANNER_ART = r"""
   _____
  |  __ \
  | |__) |_   _ _ __
  |  _  /| | | | '_ \
  | | \ \| |_| | | |
  |_|  \_\\__,_|_| |_|
"""


def print_welcome() -> None:
    commands = Table.grid(padding=(0, 2))
    commands.add_column(style="bold cyan", no_wrap=True)
    commands.add_column(style="dim")
    for command, description in (
        ("/plan", "read-only planning workflow"),
        ("/skills", "list reusable skills"),
        ("/skill-create", "create a reusable skill"),
        ("/skill-stats", "show skill evolution stats"),
        ("/memory", "list long-term memories"),
        ("/compact", "compact current context"),
        ("exit", "quit the session"),
    ):
        commands.add_row(command, description)
    body = Table.grid()
    body.add_row(Align.center(Text(BANNER_ART, style="bold #d19a66")))
    body.add_row(Align.center(Text("Run Agent", style="bold #f6c177")))
    body.add_row(Align.center(Text("Evolvable Coding Agent CLI", style="bold cyan")))
    body.add_row("")
    body.add_row(Panel(commands, title="Quick Commands", border_style="cyan", box=box.ROUNDED))
    console.print()
    console.print(Panel(body, title="[bold #f6c177] run-agent ready [/bold #f6c177]", subtitle="[dim]Type your request below[/dim]", border_style="#d19a66", box=box.ROUNDED, padding=(1, 2)))
    console.print()


def print_user_prompt() -> None:
    console.print("\n[bold #f6c177]Run[/bold #f6c177][bold cyan]Agent[/bold cyan] [dim]❯[/dim] ", end="")


def print_assistant_text(text: str) -> None:
    _safe_stdout_write(text)


def print_error(msg: str) -> None:
    console.print(Panel(_safe_text(msg), title="[bold red]Error[/bold red]", border_style="red", box=box.ROUNDED, padding=(0, 1)))


def print_info(msg: str) -> None:
    console.print(Panel(_safe_text(msg), title="[bold cyan]Info[/bold cyan]", border_style="cyan", box=box.ROUNDED, padding=(0, 1)))


def print_warning(msg: str) -> None:
    console.print(Panel(_safe_text(msg), title="[bold yellow]Notice[/bold yellow]", border_style="yellow", box=box.ROUNDED, padding=(0, 1)))


def print_goodbye() -> None:
    console.print(Panel(Text("Bye. Run Agent saved for next time.", style="bold #f6c177"), border_style="#d19a66", box=box.ROUNDED, padding=(0, 1)))


def print_interrupted() -> None:
    print_warning("Interrupted. Press Ctrl+C again to exit.")


def print_memory_entries(memories: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Type", style="bold #f6c177", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    for memory in memories:
        table.add_row(_safe_text(getattr(memory, "type", "")), _safe_text(getattr(memory, "name", "")), _safe_text(getattr(memory, "description", "")))
    console.print(Panel(table, title="[bold cyan]Memories[/bold cyan]", border_style="cyan", box=box.ROUNDED))


def print_skill_entries(skills: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Skill", style="bold #f6c177", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Mode", style="magenta", no_wrap=True)
    table.add_column("Description", style="white")
    for skill in skills:
        name = getattr(skill, "name", "")
        table.add_row(_safe_text(f"/{name}" if getattr(skill, "user_invocable", False) else name), _safe_text(getattr(skill, "source", "")), _safe_text(getattr(skill, "context", "")), _safe_text(getattr(skill, "description", "")))
    console.print(Panel(table, title="[bold cyan]Skills[/bold cyan]", border_style="cyan", box=box.ROUNDED))


def print_plan_for_approval(content: str) -> None:
    console.print(Panel(_safe_text(content), title="[bold cyan]Plan[/bold cyan]", border_style="cyan", box=box.ROUNDED))


def print_plan_approval_options() -> None:
    console.print("[1] Approve  [2] Reject  [3] Revise  [4] Cancel")


def print_cost(input_tokens: int, output_tokens: int, *, input_cost_per_million: float = 3.0, output_cost_per_million: float = 15.0) -> None:
    total = input_tokens * input_cost_per_million / 1_000_000 + output_tokens * output_cost_per_million / 1_000_000
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="white")
    table.add_row("input", f"{input_tokens} tokens")
    table.add_row("output", f"{output_tokens} tokens")
    table.add_row("estimate", f"${total:.4f}")
    console.print(Panel(table, title="Cost", border_style="cyan", box=box.ROUNDED))
