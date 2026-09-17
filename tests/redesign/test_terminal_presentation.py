"""Presentation regressions using local providers and real terminal components."""

import asyncio
from contextlib import suppress
from io import StringIO

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console

from run_agent_coding.application import CodingApplication
from run_agent_coding.commands import CommandResult, SlashCommand
from run_agent_coding.terminal import Terminal
from run_agent_core.events import (
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import TextDeltaEvent
from run_agent_core.tools import AgentToolResult

from .test_coding_application import ReplyProvider, options


def make_terminal(app, width=80):
    output = StringIO()
    terminal = Terminal(
        app,
        console=Console(file=output, width=width, height=25, color_system=None),
        terminal_input=DummyInput(),
        terminal_output=DummyOutput(),
    )
    return terminal, output


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_welcome_and_tool_preview_fit_and_expand_preserves_full_text(tmp_path, width):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        terminal, output = make_terminal(app, width)
        terminal._welcome()
        terminal.render(
            ToolExecutionStartEvent(
                tool_call_id="read-one",
                tool_name="read",
                args={"path": "[red]src/项目/example.py", "credential": "do-not-display"},
            )
        )
        full = "\n".join(["[red]literal[/red] " + "long " * 30] + [f"line-{i}" for i in range(8)])
        terminal.render(
            ToolExecutionEndEvent(
                tool_call_id="read-one",
                tool_name="read",
                result=AgentToolResult(content=[TextContent(text=full)]),
                is_error=True,
            )
        )
        transcript = output.getvalue()
        assert "Run Agent" in transcript
        assert "[red]" in transcript
        assert "do-not-display" not in transcript
        assert "Error" in transcript and "/expand read-one" in transcript
        assert "line-7" not in transcript
        assert all(cell_len(line) <= width for line in transcript.splitlines())
        terminal._expand("read-one")
        assert "line-7" in output.getvalue()
        assert terminal._tools["read-one"] == full


async def test_completions_follow_current_registry_and_never_complete_prompt_arguments(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        terminal, _ = make_terminal(app)
        app.session.command_registry.register(
            SlashCommand(
                name="extension-demo",
                description="An extension added after the editor was created.",
                usage="/extension-demo",
                handler=lambda context: CommandResult(handled=True),
                aliases=("demo",),
            )
        )
        completer = terminal.editor.completer
        completion = list(completer.get_completions(Document("/extension"), CompleteEvent()))
        assert len(completion) == 1
        assert completion[0].text == "/extension-demo"
        assert "after the editor" in completion[0].display_meta_text
        assert list(completer.get_completions(Document("/queue /ex"), CompleteEvent())) == []
        assert list(completer.get_completions(Document("explain /ex"), CompleteEvent())) == []
        names = {item.text for item in completer.get_completions(Document("/"), CompleteEvent())}
        assert {"/demo", "/expand", "/stop", "/queue", "/context"} <= names


async def test_stream_uses_live_view_and_commits_canonical_message_once(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        terminal, output = make_terminal(app, width=40)
        first_line = "完整中文回答与 English " * 8
        tail = "最后一行没有换行，也必须完整保留。"
        text = first_line + "\n" + tail
        message = AssistantMessage(content=[TextContent(text=text)])
        for delta in (first_line[:7], first_line[7:], "\n" + tail):
            terminal.render(
                MessageUpdateEvent(
                    message=message,
                    assistant_message_event=TextDeltaEvent(
                        content_index=0, delta=delta, partial=message
                    ),
                )
            )
            # In-progress text belongs to the editor, never to shared stdout.
            assert "完整中文" not in output.getvalue()
        assert text in "".join(value for _, value in terminal._response_fragments())
        assert tail not in output.getvalue()
        final = text + "\n最终消息补充的内容"
        terminal.render(MessageEndEvent(message=AssistantMessage(content=final)))
        assert output.getvalue().count(final) == 1
        assert terminal._response_text == ""
        terminal.render(MessageEndEvent(message=AssistantMessage(content="## Result\n**Done**")))
        assert "Result" in output.getvalue()
        assert "## Result" not in output.getvalue()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_interrupted_response_preserves_partial_text_before_error(
    tmp_path, monkeypatch, cancelled
):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        terminal, output = make_terminal(app)
        partial = "中断前已经生成的完整文本 without a newline"
        message = AssistantMessage(content=partial)

        async def interrupted(text):
            yield MessageUpdateEvent(
                message=message,
                assistant_message_event=TextDeltaEvent(
                    content_index=0, delta=partial, partial=message
                ),
            )
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("model disconnected")

        monkeypatch.setattr(app, "prompt", interrupted)
        await terminal._consume("test interrupted response")
        transcript = output.getvalue()
        assert transcript.count(partial) == 1
        marker = "Stopped." if cancelled else "RuntimeError: model disconnected"
        assert transcript.index(partial) < transcript.index(marker)
        assert terminal._response_text == ""


async def test_toolbar_uses_plain_extension_text_and_reports_context(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        terminal, _ = make_terminal(app)
        terminal.ui.set_status("extension", "state", "[red]ready\n\x1bstatus")
        toolbar = "".join(text for _, text in terminal._toolbar())
        assert "Ready" in toolbar and "test" in toolbar and "ctx ~" in toolbar
        assert "[red]ready status" in toolbar
        assert "\x1b" not in toolbar
        assert "Alt+Enter" in toolbar


@pytest.mark.parametrize("width", [40, 80, 120])
async def test_real_editor_keeps_stream_and_completions_separate(tmp_path, width):
    class SizedOutput(DummyOutput):
        def get_size(self):
            return Size(rows=24, columns=width)

    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=SizedOutput(),
            )
            message = AssistantMessage(
                content="\n".join([f"完整历史第 {i} 行" for i in range(30)])
                + "\n实时回答 live text"
            )
            terminal.render(
                MessageUpdateEvent(
                    message=message,
                    assistant_message_event=TextDeltaEvent(
                        content_index=0, delta=message.text, partial=message
                    ),
                )
            )
            ready = asyncio.Event()

            def rendered(application):
                if terminal.editor.default_buffer.complete_state:
                    ready.set()

            terminal.editor.app.after_render += rendered
            prompt = asyncio.create_task(terminal._prompt("❯ "))
            try:
                pipe.send_text("/ex")
                await asyncio.wait_for(ready.wait(), 5)
                screen = terminal.editor.app.renderer.last_rendered_screen
                assert screen is not None
                lines = [
                    "".join(screen.data_buffer[row][col].char for col in range(width))
                    for row in range(screen.height)
                ]
                assert any("┌" in line for line in lines)
                assert any("/expand" in line for line in lines)
                assert any("实时回答 live text" in line for line in lines)
                top = next(i for i, line in enumerate(lines) if "┌" in line)
                bottom = next(i for i, line in enumerate(lines) if "└" in line)
                assert bottom - top < 10
                assert "实时回答" in "\n".join(lines[:top])
                assert terminal.editor.default_buffer.text == "/ex"
                terminal.render(MessageEndEvent(message=message))
                terminal.editor.app._redraw()
                assert terminal.editor.default_buffer.text == "/ex"
                assert terminal._response_text == ""
            finally:
                prompt.cancel()
                with suppress(asyncio.CancelledError):
                    await prompt
