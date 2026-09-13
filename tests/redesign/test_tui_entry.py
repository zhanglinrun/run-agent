"""Interactive frontend selection must preserve application ownership."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from run_agent_coding import cli


@pytest.mark.parametrize("use_tui", [True, False])
async def test_interactive_frontend_receives_live_application_and_is_closed(monkeypatch, use_tui):
    application = object()
    closed = []

    @asynccontextmanager
    async def lifetime():
        try:
            yield application
        finally:
            closed.append(True)

    monkeypatch.setattr(cli.CodingApplication, "open", AsyncMock(return_value=lifetime()))
    tui_run = AsyncMock()
    terminal_run = AsyncMock()
    monkeypatch.setitem(
        cli.sys.modules, "run_agent_coding.tui", SimpleNamespace(run_tui_app=tui_run)
    )
    monkeypatch.setitem(
        cli.sys.modules,
        "run_agent_coding.terminal",
        SimpleNamespace(Terminal=lambda app: SimpleNamespace(run=terminal_run)),
    )
    assert await cli._run(
        SimpleNamespace(), "hello", print_mode=False, output=cli.OutputFormat.text, tui=use_tui
    )
    if use_tui:
        tui_run.assert_awaited_once_with(application, initial_prompt="hello")
        terminal_run.assert_not_awaited()
    else:
        terminal_run.assert_awaited_once_with("hello")
        tui_run.assert_not_awaited()
    assert closed == [True]


@pytest.mark.parametrize("extra, expected", [([], {}), (["--no-tui"], {"tui": False})])
def test_cli_selects_default_tui_or_explicit_fallback(monkeypatch, tmp_path, extra, expected):
    run = AsyncMock(return_value=True)
    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)))
    result = CliRunner().invoke(cli.app, ["--cwd", str(tmp_path), *extra])
    assert result.exit_code == 0, result.output
    assert run.await_args.kwargs == {
        "print_mode": False,
        "output": cli.OutputFormat.text,
        **expected,
    }
