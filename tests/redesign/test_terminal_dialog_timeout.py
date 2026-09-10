"""A timed-out dialog must return the terminal to the main prompt.

This covers the dialog-teardown contract, which currently holds: when
``ui.input(timeout=...)`` expires while its dialog is open, the terminal does
come back to the main prompt, and a second answered dialog before it changes
nothing.

It deliberately does NOT reproduce the intermittent lost-input failure recorded
in ``docs/implementation/open-issue-flake-01.md``. That failure shows up under
pytest at roughly one run in twelve, while these orderings pass every time, so
this file records what has been ruled out rather than what is broken.
"""

import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.terminal import Terminal

TIMEOUT_SECONDS = 0.5


def arm_dialogs(terminal) -> list[asyncio.Event]:
    """Record the order in which dialogs actually reach their prompt."""
    opened = [asyncio.Event(), asyncio.Event()]
    reached = iter(opened)
    dialog_prompt = terminal._dialog_prompt

    async def observe(request):
        event = next(reached, None)
        if event is not None:
            event.set()
        return await dialog_prompt(request)

    terminal._dialog_prompt = observe
    return opened


async def test_a_timed_out_dialog_returns_the_terminal_to_the_main_prompt(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            opened = arm_dialogs(terminal)
            running = asyncio.create_task(terminal.run())

            answered = asyncio.create_task(terminal.ui.input("API key", secret=True))
            await asyncio.wait_for(opened[0].wait(), 5)
            pipe.send_text("value\r")
            assert await asyncio.wait_for(answered, 5) == "value"

            expiring = asyncio.create_task(terminal.ui.input("Timeout", timeout=TIMEOUT_SECONDS))
            await asyncio.wait_for(opened[1].wait(), 5)
            assert await asyncio.wait_for(expiring, 5) is None

            pipe.send_text("/quit\r")
            await asyncio.wait_for(running, 5)
