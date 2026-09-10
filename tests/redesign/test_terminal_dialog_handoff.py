"""RED: a timed-out dialog must hand input over, not lose it (T-002).

docs/implementation/flake-root-cause.md proves the ordering with tracing: the caller
resumes from ``ui.input(timeout=...)`` in the same instant that the dialog reader is
cancelled and the main prompt is armed. Input arriving in that window is consumed by
the dying reader and lost, which is what makes the gate flaky about 0.7% of the time
in isolation and far more often under the full suite.

Racing for a 0.7% window would make a worthless test, so the teardown is widened on
purpose: the dialog reader lingers after cancellation, which makes the window every
single run. Two things are then asserted.

  1. the invariant - when the caller resumes, the main prompt is already the reader
  2. the consequence - the next line written reaches the main prompt, not the void

Test 1 is the discriminating one: it fails every run against the current ordering and
is what the fix must turn green. Test 2 is a guard rather than a discriminator - it
already passes today, because widening the window after cancellation leaves the line
sitting in the pipe instead of eating it. It is kept because it pins the user-visible
property the fix must not break, and it is not claimed to prove the fix.
"""

import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.terminal import Terminal

TEARDOWN_LAG = 0.2


class Harness:
    """A terminal whose dialog reader lingers after cancellation."""

    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal
        self.dialog_entered = asyncio.Event()
        self.dialog_still_reading = False
        self.answers: list[str] = []
        self._real_prompt = terminal._prompt
        self._real_dialog = terminal._dialog_prompt
        terminal._prompt = self._prompt
        terminal._dialog_prompt = self._dialog

    async def _prompt(self, label: str) -> str:
        answer = await self._real_prompt(label)
        self.answers.append(answer)
        return answer

    async def _dialog(self, dialog) -> str:
        """Mark the dialog reader alive for its whole life, including teardown."""
        self.dialog_entered.set()
        self.dialog_still_reading = True
        try:
            return await self._real_dialog(dialog)
        except BaseException:
            await asyncio.sleep(TEARDOWN_LAG)
            raise
        finally:
            self.dialog_still_reading = False

    async def wait_for_answer(self, wanted: str, *, seconds: float = 5.0) -> bool:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if wanted in self.answers:
                return True
            await asyncio.sleep(0.01)
        return False


async def test_a_timed_out_dialog_does_not_release_the_caller_while_still_reading(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            harness = Harness(terminal)
            running = asyncio.create_task(terminal.run())

            expiring = asyncio.create_task(terminal.ui.input("Timeout", timeout=0.3))
            await asyncio.wait_for(harness.dialog_entered.wait(), 5)
            assert await asyncio.wait_for(expiring, 5) is None

            # The root cause: the caller is released while the dialog reader is still
            # alive, so input arriving now is read by the reader that is going away.
            assert harness.dialog_still_reading is False, (
                "the timed-out dialog released the caller while its reader was still alive"
            )
            running.cancel()


async def test_a_line_written_after_a_timed_out_dialog_reaches_the_main_prompt(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            harness = Harness(terminal)
            running = asyncio.create_task(terminal.run())

            expiring = asyncio.create_task(terminal.ui.input("Timeout", timeout=0.3))
            await asyncio.wait_for(harness.dialog_entered.wait(), 5)
            assert await asyncio.wait_for(expiring, 5) is None

            pipe.send_text("hello\r")
            assert await harness.wait_for_answer("hello"), (
                f"the line was lost; the prompt only saw {harness.answers!r}"
            )
            running.cancel()
