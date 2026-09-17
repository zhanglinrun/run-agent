import asyncio

import pytest

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.session_manager import SessionManager
from run_agent_core.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session import CustomEntry, LeafEntry, resolve_active_leaf_id
from run_agent_core.session.contracts import SessionConflict


class ReplyProvider:
    async def stream_response(self, *, messages, **kwargs):
        last = next(
            (message.text for message in reversed(messages) if isinstance(message, UserMessage)), ""
        )
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text=f"reply: {last}")],
                model="test",
                provider="test",
                stop_reason="stop",
            ),
        )


class RecordingProvider(ReplyProvider):
    def __init__(self):
        self.requests = []

    async def stream_response(self, *, model, system, messages, tools, **kwargs):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                        "execution_mode": tool.execution_mode,
                    }
                    for tool in tools
                ],
            }
        )
        async for event in super().stream_response(messages=messages, **kwargs):
            yield event


def options(tmp_path, **kwargs):
    return ApplicationOptions(
        cwd=tmp_path,
        paths=RunAgentPaths(home=tmp_path / "state", agents_home=tmp_path / "agents"),
        model="test",
        provider_name="test",
        extensions_enabled=False,
        **kwargs,
    )


async def test_application_commits_receipt_reopens_and_branches(tmp_path):
    opts = options(tmp_path)
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        events = [event async for event in app.prompt("first")]
        receipt = events[-1]
        assert isinstance(receipt, AgentSettledEvent)
        assert receipt.status == "succeeded"
        session_id = app.session.session_id
        entries = (await app.session.storage.read_entries()).entries
        assert all(entry.seq is not None for entry in entries)
        assert resolve_active_leaf_id(entries) == receipt.head_id
        assert isinstance(entries[-1], LeafEntry)
        head_entry = next(entry for entry in entries if entry.id == receipt.head_id)
        assert head_entry.message.text == "reply: first"
        first_head = receipt.head_id
        first_branch = receipt.branch_id
        events = [event async for event in app.prompt("second")]
        second_head = events[-1].head_id
        await app.command(f"/branch {first_head}")
        assert app.session.storage.branch_id != first_branch
        assert [m.text for m in app.session.messages if isinstance(m, UserMessage)] == ["first"]
        await app.command("/name JSONL session")
        events = [event async for event in app.prompt("third")]
        third_head = events[-1].head_id
        jsonl = opts.paths.project_session_dir(tmp_path) / f"{session_id}.jsonl"
        assert jsonl.is_file() and jsonl.stat().st_size > 0
    async with await CodingApplication.open(
        options(tmp_path, resume=session_id), provider=ReplyProvider()
    ) as reopened:
        assert reopened.session.session_title == "JSONL session"
        assert (await reopened.session.storage.get_head()).entry_id == third_head
        assert [m.text for m in reopened.session.messages if isinstance(m, UserMessage)] == [
            "first",
            "third",
        ]
        assert second_head in {
            entry.id for entry in (await reopened.session.storage.read_entries()).entries
        }


async def test_failed_branch_summary_keeps_original_active_head(tmp_path):
    manager = SessionManager(options(tmp_path).paths)
    try:
        record = await manager.create_session(cwd=tmp_path, model="test")
        storage = await manager.open_storage(record.id)
        entry = CustomEntry(namespace="test", data={})
        await storage.append_entries((entry,), expected_head=None, token=storage.token)
        original = await storage.get_head()
        invalid = CustomEntry(parent_id="missing", namespace="test", data={})
        with pytest.raises(SessionConflict):
            await storage.fork(entry.id, token=storage.token, entries=(invalid,))
        assert await storage.get_head() == original
    finally:
        await manager.aclose()


class WaitingProvider:
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream_response(self, **kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
            yield  # pragma: no cover
        finally:
            self.closed.set()


async def test_cancelled_run_lets_the_next_run_start(tmp_path):
    provider = WaitingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:

        async def consume():
            return [event async for event in app.prompt("cancel this")]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(provider.entered.wait(), 5)
        run_token = app.session.storage.token
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.closed.is_set()
        assert not app.session.is_running
        assert app.session.storage.token.generation > run_token.generation


async def test_cancelled_open_releases_new_writer(tmp_path, monkeypatch):
    manager = SessionManager(options(tmp_path).paths)
    try:
        record = await manager.create_session(cwd=tmp_path, model="test")
        original = manager._open_storage
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def paused_open(*args, **kwargs):
            handle = await original(*args, **kwargs)
            acquired.set()
            await release.wait()
            return handle

        monkeypatch.setattr(manager, "_open_storage", paused_open)
        task = asyncio.create_task(manager.open_storage(record.id))
        await asyncio.wait_for(acquired.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(manager, "_open_storage", original)
        storage = await manager.open_storage(record.id)
        assert not storage.closed
    finally:
        await manager.aclose()


async def test_generator_close_does_not_leave_session_running(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        events = app.prompt("close early")
        await anext(events)
        await events.aclose()
        assert not app.session.is_running


async def test_reopen_keeps_persisted_entries_without_replaying_work(tmp_path):
    manager = SessionManager(options(tmp_path).paths)
    record = await manager.create_session(cwd=tmp_path, model="test")
    storage = await manager.open_storage(record.id)
    await storage.begin_run("unfinished")
    entry = CustomEntry(namespace="test", data={"kept": True})
    await storage.append_entries((entry,), expected_head=None, token=storage.token)
    await manager.aclose()
    manager = SessionManager(options(tmp_path).paths)
    try:
        storage = await manager.open_storage(record.id)
        assert [item.id for item in (await storage.read_entries()).entries] == [entry.id]
        await storage.begin_run("new-explicit-request")
    finally:
        await manager.aclose()


async def test_real_read_tool_result_and_final_answer_survive_reopen(tmp_path):
    (tmp_path / "source.txt").write_text("verified file contents", encoding="utf-8")

    class ToolProvider:
        async def stream_response(self, *, messages, **kwargs):
            result = next(
                (
                    message
                    for message in reversed(messages)
                    if isinstance(message, ToolResultMessage)
                ),
                None,
            )
            if result is None:
                yield AssistantDoneEvent(
                    reason="toolUse",
                    message=AssistantMessage(
                        content=[
                            ToolCall(id="read-one", name="read", arguments={"path": "source.txt"})
                        ],
                        stop_reason="toolUse",
                        model="test",
                    ),
                )
            else:
                assert not result.is_error
                assert "verified file contents" in result.text
                yield AssistantDoneEvent(
                    reason="stop",
                    message=AssistantMessage(
                        content=[TextContent(text="read verified")],
                        stop_reason="stop",
                        model="test",
                    ),
                )

    async with await CodingApplication.open(options(tmp_path), provider=ToolProvider()) as app:
        events = [event async for event in app.prompt("read the file")]
        assert events[-1].status == "succeeded"
        identity = app.session.session_id
        assert any(event.type == "tool_execution_end" for event in events)
    async with await CodingApplication.open(
        options(tmp_path, resume=identity), provider=ReplyProvider()
    ) as app:
        assert [message.role for message in app.session.messages] == [
            "user",
            "assistant",
            "toolResult",
            "assistant",
        ]
        assert app.session.messages[-1].text == "read verified"
