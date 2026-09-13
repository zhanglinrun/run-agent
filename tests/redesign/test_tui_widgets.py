"""Headless interaction coverage for the real Textual components."""

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Collapsible, Input, Markdown, Static

from run_agent_coding.tui.screens import ConfirmScreen, InputScreen, OutputScreen, SelectionScreen
from run_agent_coding.tui.state import ChatItem
from run_agent_coding.tui.themes import RUN_DARK
from run_agent_coding.tui.widgets import MessageBlock, PromptInput, SessionSidebar, TranscriptView


class WidgetApp(App[None]):
    CSS = """
    #main { height: 1fr; }
    #conversation { width: 1fr; }
    PromptInput { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.completion_moves: list[int] = []
        self.accepted = 0
        self.register_theme(RUN_DARK)
        self.theme = RUN_DARK.name

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="conversation"):
                yield TranscriptView(id="transcript")
                yield PromptInput(id="prompt")
            yield SessionSidebar(id="sidebar")

    def on_prompt_input_submitted(self, message: PromptInput.Submitted) -> None:
        self.submitted.append(message.text)

    def on_prompt_input_completion_moved(self, message: PromptInput.CompletionMoved) -> None:
        self.completion_moves.append(message.direction)

    def on_prompt_input_completion_accepted(self, message: PromptInput.CompletionAccepted) -> None:
        self.accepted += 1


@pytest.mark.parametrize("width", [80, 120])
async def test_full_transcript_stream_replace_tools_and_follow(width: int) -> None:
    app = WidgetApp()
    async with app.run_test(size=(width, 32)) as pilot:
        view = app.query_one(TranscriptView)
        app.query_one(SessionSidebar).update_info(
            "MODEL\ngpt-5.6-luna\n\nWORKSPACE\nE:/项目/run-agent\n\nTOKENS\n12,340\n\nTOOLS\nRead · Write · Bash"
        )
        user = ChatItem("user", "user", "请分析这个项目，并完整显示所有结果。")
        reply = ChatItem("answer", "assistant", "## 项目分析\n\n流式输出", pending=True)
        await view.sync_items([user, reply])
        block = view._blocks["answer"]
        reply.text += "包含完整的中文内容。\n\n```python\nprint('你好')\n```"
        await view.sync_items([user, reply])
        reply.text = "## 项目分析\n\n最终内容替换流式草稿。\n\n| 模块 | 作用 |\n|---|---|\n| Core | 推理循环 |\n\n```python\nprint('完整结果')\n```"
        reply.pending = False
        tool = ChatItem(
            "tool",
            "tool",
            tool_name="read",
            arguments={"path": "src/[core].py"},
            result_text="\n".join(f"第 {i} 行：完整工具输出" for i in range(100)),
        )
        await view.sync_items([user, tool, reply])
        await pilot.pause()
        assert view._blocks["answer"] is block
        assert block._stream is None
        assert block.query_one(Markdown)._markdown == reply.text
        assert len(view.query(MessageBlock)) == 3
        tool_block = view._blocks["tool"]
        assert tool_block.query_one(Collapsible).collapsed
        assert str(tool_block.query_one(".tool-output", Static).content) == tool.result_text
        view.set_tools_expanded(True)
        await pilot.pause(0.4)
        assert not tool_block.query_one(Collapsible).collapsed
        view.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert not view.is_vertical_scroll_end
        reply.text += "\n\n新增内容不应抢走用户的滚动位置。"
        await view.sync_items([user, tool, reply])
        await pilot.pause()
        assert view.scroll_y == 0
        view.set_tools_expanded(False)
        await view.scroll_latest()
        app.query_one(PromptInput).focus()
        await pilot.pause()
        artifacts = Path(".run/validation/tui")
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f"components-{width}.svg").write_text(
            app.export_screenshot(), encoding="utf-8"
        )
        await view.sync_items([reply])
        assert len(view.query(MessageBlock)) == 1
        await view.sync_items([])
        assert view.query_one("#transcript-welcome", Static).display


async def test_prompt_newlines_history_completion_and_submission() -> None:
    app = WidgetApp()
    async with app.run_test() as pilot:
        prompt = app.query_one(PromptInput)
        prompt.focus()
        prompt.insert("中文任务")
        await pilot.press("ctrl+j")
        prompt.insert("第二行")
        await pilot.press("enter")
        assert app.submitted == ["中文任务\n第二行"]
        assert prompt.text == ""
        prompt.insert("尚未提交的草稿")
        await pilot.press("up")
        assert prompt.text == "中文任务\n第二行"
        await pilot.press("down")
        assert prompt.text == "中文任务\n第二行"
        await pilot.press("down")
        assert prompt.text == "尚未提交的草稿"
        prompt.completion_active = True
        await pilot.press("up", "down", "tab", "enter")
        assert app.completion_moves == [-1, 1]
        assert app.accepted == 2
        assert len(app.submitted) == 1


async def test_search_selection_cancel_secret_and_output() -> None:
    app = WidgetApp()
    results: list[str | None] = []
    confirmations: list[bool] = []
    async with app.run_test(size=(80, 30)) as pilot:
        await app.push_screen(
            SelectionScreen("选择模型", ["模型甲 [default]", "模型乙", "Other"]), results.append
        )
        app.screen.query_one(Input).value = "模型"
        await pilot.pause()
        dialog = app.screen.query_one(".dialog").region
        assert dialog.x > 0 and dialog.y > 0
        await pilot.press("down", "enter")
        assert results == ["模型乙"]
        await app.push_screen(SelectionScreen("Empty", []), results.append)
        await pilot.press("enter", "escape")
        assert results[-1] is None
        await app.push_screen(InputScreen("API key", secret=True), results.append)
        field = app.screen.query_one(Input)
        assert field.password
        field.value = "secret-value"
        await pilot.press("enter")
        assert results[-1] == "secret-value"
        assert field.value == ""
        await app.push_screen(ConfirmScreen("确认", "允许修改文件？"), confirmations.append)
        await pilot.press("escape")
        assert confirmations == [False]
        output = "\n".join(f"Output {i}" for i in range(200))
        await app.push_screen(OutputScreen("Full output", output))
        assert str(app.screen.query_one("#output-text", Static).content) == output
        await pilot.press("escape")
