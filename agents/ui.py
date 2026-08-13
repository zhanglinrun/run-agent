"""Terminal UI for Run Agent."""

from __future__ import annotations

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


def print_welcome() -> None:
    console.print()
    console.print("[bold cyan]Run Agent[/bold cyan] — Coding Agent CLI")
    console.print(
        "[dim]命令: /help  /clear  /cost  /sessions  /resume  /plan  /memory  /skills  "
        "/compact  /mcp  /extract_now  /skill-evolve  /skills-stats  /exit[/dim]"
    )
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


def print_plan_for_approval(plan_content: str) -> None:
    lines = plan_content.splitlines()
    preview = "\n".join(lines[:60])
    if len(lines) > 60:
        preview += f"\n\n... ({len(lines) - 60} more lines)"
    console.print(
        Panel(
            Text(preview or "(empty plan)"),
            title="[bold cyan]Plan for Approval[/bold cyan]",
            border_style="cyan",
        )
    )


def print_plan_approval_options() -> None:
    console.print(
        Panel(
            "1  Clear context and execute  (acceptEdits, fresh)\n"
            "2  Execute                   (acceptEdits, keep context)\n"
            "3  Manually approve edits    (restore previous mode)\n"
            "4  Keep planning             (revise with feedback)",
            title="[bold yellow]Choose 1-4[/bold yellow]",
            border_style="yellow",
        )
    )


def print_memory_entries(memories: list[object]) -> None:
    table = Table(box=ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Type", style="bold yellow", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    for m in memories:
        table.add_row(
            str(getattr(m, "type", "") or ""),
            str(getattr(m, "name", "") or ""),
            str(getattr(m, "description", "") or ""),
        )
    console.print(
        Panel(table, title="[bold cyan]Memories[/bold cyan]", border_style="cyan", box=ROUNDED)
    )


def print_skills(skills: list[object]) -> None:
    table = Table(box=ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Source", style="bold yellow", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Mode", style="dim", no_wrap=True)
    table.add_column("Description", style="dim")
    for s in skills:
        invocable = bool(getattr(s, "user_invocable", True))
        table.add_row(
            str(getattr(s, "source", "") or ""),
            str(getattr(s, "name", "") or ""),
            "user" if invocable else "auto",
            str(getattr(s, "description", "") or ""),
        )
    console.print(
        Panel(table, title="[bold cyan]Skills[/bold cyan]", border_style="cyan", box=ROUNDED)
    )


def print_sub_agent_start(agent_type: str, description: str) -> None:
    console.print(
        Panel(
            description or "(no description)",
            title=f"[bold magenta]Sub-agent started: {agent_type}[/bold magenta]",
            border_style="magenta",
            box=ROUNDED,
            padding=(0, 1),
        )
    )


def print_sub_agent_end(agent_type: str, _description: str = "") -> None:
    console.print(
        Panel(
            "completed",
            title=f"[bold magenta]Sub-agent finished: {agent_type}[/bold magenta]",
            border_style="magenta",
            box=ROUNDED,
            padding=(0, 1),
        )
    )
