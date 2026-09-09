import asyncio
from dataclasses import replace
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import ExtensionError
from run_agent_coding.terminal import Terminal, TerminalUi

from .test_coding_application import ReplyProvider, WaitingProvider, options


async def test_terminal_editor_multiline_stream_and_exit(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            output = StringIO()
            terminal = Terminal(
                app,
                console=Console(file=output, color_system=None),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            finished = asyncio.Event()
            original = terminal.render

            def render(event):
                original(event)
                if isinstance(event, AgentSettledEvent):
                    finished.set()

            terminal.render = render
            task = asyncio.create_task(terminal.run())
            pipe.send_text("one\x1b\rtwo\r")
            await asyncio.wait_for(finished.wait(), 5)
            pipe.send_text("/quit\r")
            await asyncio.wait_for(task, 5)
            assert "reply: one\ntwo" in output.getvalue()
            assert "succeeded" in output.getvalue()


async def test_terminal_ctrl_c_drains_run_then_quits(tmp_path):
    provider = WaitingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(terminal.run("wait"))
            await asyncio.wait_for(provider.entered.wait(), 5)
            pipe.send_text("\x03")
            await asyncio.wait_for(provider.closed.wait(), 5)
            # Stopping owns its final database commit before the next input is consumed.
            pipe.send_text("/quit\r")
            await asyncio.wait_for(task, 5)
            assert not app.session.is_running


async def test_secret_dialog_does_not_pollute_history_and_timeout_restores_editor(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            output = StringIO()
            terminal = Terminal(
                app,
                console=Console(file=output),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(terminal.run())
            answer_task = asyncio.create_task(terminal.ui.input("API key", secret=True))
            original = terminal._dialog_prompt
            shown = asyncio.Event()

            async def dialog(request):
                shown.set()
                return await original(request)

            terminal._dialog_prompt = dialog
            await asyncio.wait_for(shown.wait(), 5)
            pipe.send_text("secret-test-value\r")
            assert await asyncio.wait_for(answer_task, 5) == "secret-test-value"
            assert "secret-test-value" not in terminal.editor.history.get_strings()
            assert "secret-test-value" not in terminal.dialog_editor.history.get_strings()
            assert "secret-test-value" not in output.getvalue()
            assert await terminal.ui.input("Timeout", timeout=0.05) is None
            pipe.send_text("/quit\r")
            await asyncio.wait_for(task, 5)


async def test_extension_status_is_owned_by_source_and_old_api_retires(tmp_path):
    extension = tmp_path / "status.py"
    extension.write_text(
        'def setup(api):\n    api.on("session_start", lambda event, context: context.ui.set_status("ready", "ready"))\n',
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extensions_enabled=True, extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        ui = TerminalUi(Console(file=StringIO()))
        await app.start(ui)
        assert list(ui.status.values()) == ["ready"]
        source = next(iter(ui.status))[0]
        old_api = app.session.extension_runtime._api_for(source)
        old_api.context.ui.set_status("temporary", "old")
        await app.command("/reload")
        assert "old" not in ui.status.values()
        with pytest.raises(ExtensionError, match="stale"):
            old_api.context.ui.set_status("late", "late")
