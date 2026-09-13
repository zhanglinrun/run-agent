import asyncio

from textual.widgets import Collapsible, Input, ListView

from run_agent_coding.application import CodingApplication
from run_agent_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from run_agent_coding.tui import RunAgentTui
from run_agent_coding.tui.screens import OutputScreen
from run_agent_coding.tui.widgets import PromptInput
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent, AssistantStartEvent, TextDeltaEvent

from .test_coding_application import ReplyProvider, WaitingProvider, options


async def settled(tui):
    async with asyncio.timeout(5):
        while not (tui.ready and (tui._operation is None or tui._operation.done())):
            await asyncio.sleep(0.02)
        if tui._operation is not None:
            tui._operation.result()


async def test_tui_submit_completion_session_and_new(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        tui = RunAgentTui(app)
        async with tui.run_test(size=(120, 40)) as pilot:
            await settled(tui)
            editor = tui.query_one(PromptInput)
            editor.load_text("hello 中文")
            await pilot.press("enter")
            await settled(tui)
            assert [item.text for item in tui.state.items if item.role == "user"] == ["hello 中文"]
            assert any(item.text == "reply: hello 中文" for item in tui.state.items)
            assert not tui.state.running
            editor.load_text("/ses")
            await pilot.pause()
            assert editor.completion_active
            await pilot.press("tab")
            assert editor.text == "/session "
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(tui.screen, OutputScreen)
            await pilot.press("escape")
            await settled(tui)
            previous = app.session.session_id
            tui.submit("/new")
            await settled(tui)
            assert app.session.session_id != previous
            assert not any(item.role == "user" for item in tui.state.items)


async def test_tui_cancel_preserves_draft_and_closes_provider(tmp_path):
    provider = WaitingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        tui = RunAgentTui(app, "wait")
        async with tui.run_test(size=(80, 30)) as pilot:
            await asyncio.wait_for(provider.entered.wait(), 5)
            editor = tui.query_one(PromptInput)
            editor.load_text("next task draft")
            await pilot.press("ctrl+c")
            await asyncio.wait_for(provider.closed.wait(), 5)
            await pilot.pause()
            assert editor.text == "next task draft"
            assert not app.session.is_running
            assert not tui.state.running


async def test_tui_dialog_secret_timeout_selection_and_source_status(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        tui = RunAgentTui(app)
        async with tui.run_test(size=(100, 35)) as pilot:
            await settled(tui)
            editor = tui.query_one(PromptInput)
            editor.load_text("preserved draft")
            answer = asyncio.create_task(tui.ui.input("API key", secret=True))
            await pilot.pause()
            assert tui.screen.query_one(Input).password
            tui.screen.query_one(Input).value = "private-key-test"
            await pilot.press("enter")
            assert await answer == "private-key-test"
            assert editor.text == "preserved draft"
            assert "private-key-test" not in editor._history
            assert all("private-key-test" not in item.text for item in tui.state.items)
            timeout = asyncio.create_task(tui.ui.input("Timeout", timeout=0.1))
            await pilot.pause(0.2)
            assert await timeout is None
            assert len(tui.screen_stack) == 1
            assert tui.focused is editor
            assert editor.text == "preserved draft"
            selection = asyncio.create_task(tui.ui.select("Pick", ["first", "second"]))
            await pilot.pause()
            await pilot.press("down", "enter")
            assert await selection == "second"
            cancel = asyncio.create_task(tui.ui.confirm("Proceed", "Confirm operation"))
            await pilot.pause()
            await pilot.press("escape")
            assert await cancel is False
            tui.ui.set_status("one", "status", "one")
            tui.ui.set_status("two", "status", "two")
            tui.ui.clear_status("one")
            assert list(tui.ui.status.values()) == ["two"]


async def test_tui_stream_keeps_draft_and_full_markdown(tmp_path):
    release = asyncio.Event()
    streamed = asyncio.Event()
    full_text = "# 中文输出\n\n" + "\n\n".join(f"完整段落 {i}：不会截断。" for i in range(45))

    class StreamingProvider:
        async def stream_response(self, **kwargs):
            partial = AssistantMessage(content=[TextContent(text=full_text)])
            yield AssistantStartEvent(partial=AssistantMessage())
            yield TextDeltaEvent(content_index=0, delta=full_text, partial=partial)
            streamed.set()
            await release.wait()
            yield AssistantDoneEvent(reason="stop", message=partial)

    async with await CodingApplication.open(options(tmp_path), provider=StreamingProvider()) as app:
        await app.session.set_session_name("Stream test")
        tui = RunAgentTui(app)
        async with tui.run_test(size=(80, 30)) as pilot:
            await settled(tui)
            tui.submit("render")
            await asyncio.wait_for(streamed.wait(), 5)
            editor = tui.query_one(PromptInput)
            editor.load_text("尚未提交的草稿")
            for _ in range(100):
                if any(item.pending and item.text == full_text for item in tui.state.items):
                    break
                await asyncio.sleep(0.02)
            assert any(item.pending and item.text == full_text for item in tui.state.items)
            release.set()
            await settled(tui)
            await pilot.pause()
            assert editor.text == "尚未提交的草稿"
            assert any(not item.pending and item.text == full_text for item in tui.state.items)
            assert not tui.query_one("#sidebar").display


async def test_tui_startup_disables_input_and_shutdown_joins_run(tmp_path, monkeypatch):
    provider = WaitingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        start_release = asyncio.Event()
        original = app.start

        async def delayed_start(*args, **kwargs):
            await start_release.wait()
            await original(*args, **kwargs)

        monkeypatch.setattr(app, "start", delayed_start)
        tui = RunAgentTui(app, "wait")
        async with tui.run_test(size=(100, 35)) as pilot:
            assert tui.query_one(PromptInput).disabled
            start_release.set()
            await asyncio.wait_for(provider.entered.wait(), 5)
            assert not tui.query_one(PromptInput).disabled
            await pilot.press("ctrl+d")
            await asyncio.wait_for(provider.closed.wait(), 5)
            assert not app.session.is_running


async def test_tui_shortcuts_work_with_editor_focus_but_not_behind_dialog(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        tui = RunAgentTui(app)
        async with tui.run_test(size=(120, 35)) as pilot:
            await settled(tui)
            tui.state.add("tool", tool_name="read", result_text="complete tool output")
            await tui.sync_transcript()
            editor = tui.query_one(PromptInput)
            editor.focus()
            await pilot.press("ctrl+e")
            assert not tui.query_one(Collapsible).collapsed
            await pilot.press("ctrl+b")
            assert not tui.query_one("#sidebar").display
            await pilot.press("f2")
            assert tui.query_one("#sidebar").display
            dialog = asyncio.create_task(tui.ui.input("Input"))
            await pilot.pause()
            await pilot.press("ctrl+b", "ctrl+e")
            assert tui._sidebar_visible
            assert tui._expanded
            await pilot.press("escape")
            assert await dialog is None


async def test_tui_model_and_resume_pickers_apply_selection(tmp_path, monkeypatch):
    class LocalProvider(ReplyProvider):
        async def aclose(self):
            pass

    # A resumed session resolves its saved provider by name. Register the local
    # test endpoint and replace its transport, just as a configured host would.
    monkeypatch.setenv("RUN_TUI_TEST_API_KEY", "local-test-only")
    settings = ProviderSettings(
        default_provider="test",
        providers=(
            OpenAICompatibleProviderConfig(
                name="test",
                models=("test", "test-large"),
                default_model="test",
                api_key_env="RUN_TUI_TEST_API_KEY",
            ),
        ),
    )
    monkeypatch.setattr(
        "run_agent_coding.session._create_runtime_provider", lambda *args, **kwargs: LocalProvider()
    )
    monkeypatch.setattr("run_agent_coding.session.load_provider_settings", lambda *args: settings)
    async with await CodingApplication.open(
        options(tmp_path), provider=LocalProvider(), settings=settings
    ) as app:
        tui = RunAgentTui(app)
        async with tui.run_test(size=(120, 35)) as pilot:
            await settled(tui)
            old_session = app.session.session_id
            tui.submit("保存这条会话消息")
            await settled(tui)
            tui.submit("/new")
            await settled(tui)
            assert app.session.session_id != old_session
            tui.submit("/model")
            await pilot.pause()
            assert any("test-large" in choice for choice in tui.screen.options), tui.screen.options
            tui.screen.query_one(Input).value = "test-large"
            await pilot.pause()
            assert tui.screen._visible == ["test:test-large"]
            tui.screen.query_one(ListView).index = None
            await pilot.press("enter")
            await settled(tui)
            assert app.session.model == "test-large"
            tui.submit("/resume")
            await pilot.pause()
            tui.screen.query_one(Input).value = old_session
            await pilot.pause()
            await pilot.press("enter")
            await settled(tui)
            assert app.session.session_id == old_session, [
                item.text for item in tui.state.items if item.role == "error"
            ]
            assert any(item.text == "保存这条会话消息" for item in tui.state.items)
            assert app.session.model == "test"
