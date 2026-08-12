"""Terminal UI for Run Agent."""

from __future__ import annotations

from rich.console import Console

console = Console(highlight=False)


def print_welcome() -> None:
    console.print()
    console.print("[bold cyan]Run Agent[/bold cyan] — Coding Agent CLI")
    console.print("[dim]命令: /help   /exit[/dim]")
    console.print()


def print_user_prompt() -> None:
    console.print("[bold cyan]Run[/bold cyan] [dim]>[/dim] ", end="")


def print_assistant_text(text: str) -> None:
    console.print(text, end="")


def print_tool_call(name: str, inp: dict) -> None:
    console.print(f"\n[bold yellow]-> tool[/bold yellow] [yellow]{name}[/yellow] {inp}")


def print_tool_result(name: str, result: str) -> None:
    text = result if len(result) <= 500 else result[:500] + f"\n... ({len(result)} chars)"
    console.print(f"[dim]<- {name}[/dim]\n{text}\n")


def print_error(msg: str) -> None:
    console.print(f"[bold red]error:[/bold red] {msg}")


def print_info(msg: str) -> None:
    console.print(f"[cyan]info:[/cyan] {msg}")


def print_warning(msg: str) -> None:
    console.print(f"[yellow]warn:[/yellow] {msg}")


def print_goodbye() -> None:
    console.print("\n[dim]bye.[/dim]")


def print_interrupted() -> None:
    console.print("\n[yellow]interrupted[/yellow]")
