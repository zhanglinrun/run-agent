"""The single `run` terminal command; application behavior lives outside the UI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import load_provider_settings
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.thinking import normalize_thinking_level
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


class OutputFormat(StrEnum):
    text = "text"
    json = "json"


@app.command()
def main(
    prompt: Annotated[
        str, typer.Argument(help="Initial prompt or single request with --print.")
    ] = "",
    print_mode: Annotated[
        bool, typer.Option("--print", "-p", help="Run one request and exit.")
    ] = False,
    output: Annotated[
        OutputFormat, typer.Option("--mode", help="Print result as text or one JSON document.")
    ] = OutputFormat.text,
    cwd: Annotated[Path, typer.Option(help="Project working directory.")] = Path("."),
    state_dir: Annotated[Path | None, typer.Option(help="Application state directory.")] = None,
    provider: Annotated[str | None, typer.Option(help="Provider name.")] = None,
    model: Annotated[str | None, typer.Option(help="Model ID.")] = None,
    resume: Annotated[
        str | None, typer.Option("--session", help="Resume a SQLite session ID.")
    ] = None,
    refresh_resources: Annotated[
        bool, typer.Option(help="Explicitly adopt current resources when resuming a session.")
    ] = False,
    thinking: Annotated[str | None, typer.Option(help="Reasoning level.")] = None,
    extension: Annotated[list[Path] | None, typer.Option(help="Load a Session extension.")] = None,
    no_extensions: Annotated[bool, typer.Option(help="Disable Session extensions.")] = False,
    project_extensions: Annotated[
        bool, typer.Option(help="Discover trusted project extensions.")
    ] = False,
    trust_project: Annotated[
        bool, typer.Option(help="Trust resources in this project for this invocation.")
    ] = False,
    sessions: Annotated[bool, typer.Option(help="List stored sessions for this project.")] = False,
    providers: Annotated[bool, typer.Option(help="List configured providers.")] = False,
    login: Annotated[
        str | None, typer.Option(help="Configure provider credentials before starting a session.")
    ] = None,
) -> None:
    """Run Agent: an interactive coding harness. Also: run gateway, run bench."""
    cwd = cwd.resolve()
    if refresh_resources and resume is None:
        raise typer.BadParameter("--refresh-resources requires --session")
    if not cwd.is_dir():
        raise typer.BadParameter("Working directory does not exist", param_hint="--cwd")
    load_dotenv(cwd / ".env", override=False)
    paths = RunAgentPaths(home=state_dir.resolve()) if state_dir else RunAgentPaths()
    if login is not None:
        if not sys.stdin.isatty():
            raise typer.BadParameter("Login requires an interactive terminal.")
        from run_agent_coding.authentication import login as authenticate
        from run_agent_coding.terminal import DirectTerminalUi

        try:
            typer.echo(asyncio.run(authenticate(paths, DirectTerminalUi(), login)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise typer.Exit(130) from None
        return
    if providers:
        for item in load_provider_settings(paths).providers:
            typer.echo(item.name)
        return
    if sessions:
        asyncio.run(_list_sessions(paths, cwd))
        return
    if not print_mode and output != OutputFormat.text:
        raise typer.BadParameter("--mode requires --print", param_hint="--mode")
    if not sys.stdin.isatty():
        if not print_mode:
            raise typer.BadParameter("Use --print when stdin is redirected.")
        piped = sys.stdin.read()
        prompt = "\n\n".join(item for item in (piped, prompt) if item)
    if print_mode and not prompt.strip():
        raise typer.BadParameter("--print requires a prompt or stdin input.")
    try:
        options = ApplicationOptions(
            cwd=cwd,
            paths=paths,
            provider_name=provider,
            # `run bench` and `run gateway` both read MODEL, and this host is the one a
            # person runs by hand. Ignoring it meant a .env naming a model the endpoint
            # serves still sent DEFAULT_MODEL, and the provider's 503 named the model it
            # had used - never the one that was configured - so the cause was invisible.
            # Read after load_dotenv above, so a .env value counts.
            model=model or os.environ.get("MODEL"),
            resume=resume,
            refresh_resources=refresh_resources,
            extension_paths=tuple(extension or ()),
            extensions_enabled=not no_extensions,
            project_extensions_enabled=project_extensions,
            trust_override="approve" if trust_project else None,
            thinking=normalize_thinking_level(thinking) if thinking is not None else None,
        )
        succeeded = asyncio.run(_run(options, prompt, print_mode=print_mode, output=output))
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception as exc:
        if output == OutputFormat.json:
            typer.echo(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        else:
            typer.echo(f"{type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None
    if not succeeded:
        raise typer.Exit(1)


async def _list_sessions(paths: RunAgentPaths, cwd: Path) -> None:
    manager = SessionManager(paths)
    try:
        for record in await manager.list_sessions(cwd):
            typer.echo(f"{record.id}  {record.title or 'Untitled'}  {record.model}")
    finally:
        await manager.aclose()


async def _run(
    options: ApplicationOptions, prompt: str, *, print_mode: bool, output: OutputFormat
) -> bool:
    async with await CodingApplication.open(options) as application:
        if not print_mode:
            from run_agent_coding.terminal import Terminal

            await Terminal(application).run(prompt)
            return True
        await application.start()
        if prompt.startswith("/"):
            command = await application.command(prompt)
            if command.handled:
                payload = {
                    "status": "succeeded",
                    "session_id": application.session.session_id,
                    "text": command.message or "",
                }
                typer.echo(
                    json.dumps(payload, ensure_ascii=False)
                    if output == OutputFormat.json
                    else payload["text"]
                )
                return True
        last_text = ""
        receipt: AgentSettledEvent | None = None
        async for event in application.prompt(prompt):
            if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                last_text = event.message.text
            elif isinstance(event, AgentSettledEvent):
                receipt = event
        if receipt is None:
            raise RuntimeError("No durable completion receipt was returned")
        payload = {
            **receipt.model_dump(mode="json", by_alias=False, exclude={"type"}),
            "text": last_text,
        }
        typer.echo(
            json.dumps(payload, ensure_ascii=False) if output == OutputFormat.json else last_text
        )
        return receipt.status == "succeeded"
