"""The gateway runner drives one coding session per chat through a fake adapter."""

import asyncio

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_core.messages import AssistantMessage, TextContent, UserMessage
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_gateway.config import FeishuConfig, GatewayConfig, SessionResetPolicy
from run_agent_gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
    split_message,
)
from run_agent_gateway.run import GatewayRunner
from run_agent_gateway.session import SessionSource


class FakeAdapter(BasePlatformAdapter):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str, str | None]] = []
        self.connected = False
        self.fail_sends = 0
        self.typing: list[str] = []

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False
        await self.cancel_background_tasks()

    async def send(self, chat_id, content, *, reply_to=None, thread_id=None) -> SendResult:
        if self.fail_sends:
            self.fail_sends -= 1
            return SendResult(success=False, error="flaky")
        self.sent.append((chat_id, content, reply_to))
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def send_typing(self, event) -> None:
        self.typing.append(event.message_id or "")

    async def deliver(self, text, *, chat="chat", user="alice", chat_type="dm", message_id=None):
        event = MessageEvent(
            text=text,
            source=SessionSource("fake", chat, chat_type, user_id=user, user_name=user.title()),
            message_id=message_id or f"in-{len(self.sent)}-{text[:8]}",
        )
        await self.handle_message(event)
        return event


class BlockingProvider(ReplyProvider):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_response(self, *, messages, **kwargs):
        last = next(m.text for m in reversed(messages) if isinstance(m, UserMessage))
        if not last.startswith("Create a concise session name") and not self.release.is_set():
            self.started.set()
            await self.release.wait()
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text=f"reply: {last}")],
                model="test",
                provider="test",
                stop_reason="stop",
            ),
        )


def config(**overrides):
    feishu = FeishuConfig("app", "secret", allowed_users=frozenset({"alice"}))
    return GatewayConfig(feishu=feishu, reset_policy=SessionResetPolicy(), **overrides)


@pytest.fixture
async def runner(tmp_path):
    adapter = FakeAdapter()
    adapter.send_attempts = 2
    holder = {"provider": ReplyProvider()}
    gateway = GatewayRunner(
        config(),
        options(tmp_path),
        adapter,
        provider_factory=lambda: holder["provider"],
    )
    await gateway.start()
    try:
        yield gateway, adapter, holder
    finally:
        await gateway.stop()


async def eventually(check, *, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = check()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_text_gets_an_agent_reply_on_one_persistent_session(runner):
    gateway, adapter, _ = runner
    first = await adapter.deliver("hello")
    await adapter.wait_idle()
    assert adapter.sent == [("chat", "reply: hello", first.message_id)]
    assert adapter.typing == [first.message_id]
    entry = gateway.store.get("fake:dm:chat")
    assert entry is not None
    await adapter.deliver("again")
    await adapter.wait_idle()
    assert adapter.sent[-1][1] == "reply: again"
    assert gateway.store.get("fake:dm:chat").session_id == entry.session_id
    assert gateway.cached_session_ids == (entry.session_id,)
    record = await gateway.manager.get_session(entry.session_id)
    assert record is not None and record.cwd == gateway.options.cwd


async def test_new_status_help_and_unknown_commands(runner):
    gateway, adapter, _ = runner
    await adapter.deliver("hello")
    await adapter.wait_idle()
    old = gateway.store.get("fake:dm:chat").session_id
    await adapter.deliver("/new")
    await adapter.wait_idle()
    assert adapter.sent[-1][1].startswith("已开始新会话")
    assert gateway.store.get("fake:dm:chat").session_id != old
    assert gateway.cached_session_ids == ()
    await adapter.deliver("/status")
    await adapter.wait_idle()
    assert "会话：" in adapter.sent[-1][1] and "空闲" in adapter.sent[-1][1]
    await adapter.deliver("/help")
    await adapter.wait_idle()
    assert "/stop" in adapter.sent[-1][1]
    await adapter.deliver("/nonsense")
    await adapter.wait_idle()
    assert adapter.sent[-1][1].startswith("未知命令")


async def test_concurrent_new_commands_are_serialized(runner):
    gateway, adapter, _ = runner
    await adapter.deliver("hello")
    await adapter.wait_idle()
    old = gateway.store.get("fake:dm:chat").session_id

    await adapter.deliver("/new")
    await adapter.deliver("/reset")
    await adapter.wait_idle()

    assert gateway.store.get("fake:dm:chat").session_id != old
    assert gateway.cached_session_ids == ()
    assert sum(text.startswith("已开始新会话") for _, text, _ in adapter.sent) == 2

    gateway, adapter, holder = runner
    blocking = BlockingProvider()
    holder["provider"] = blocking
    await adapter.deliver("slow one")
    await asyncio.wait_for(blocking.started.wait(), 10)
    old = gateway.store.get("fake:dm:chat").session_id

    await adapter.deliver("/new")
    await eventually(lambda: any("已开始新会话" in text for _, text, _ in adapter.sent))
    await adapter.wait_idle()

    assert gateway.store.get("fake:dm:chat").session_id != old
    assert not any(text == "reply: slow one" for _, text, _ in adapter.sent)


async def test_unauthorized_senders_are_told_in_dm_and_ignored_in_groups(runner):
    _, adapter, _ = runner
    await adapter.deliver("hi", user="mallory")
    await adapter.wait_idle()
    assert len(adapter.sent) == 1 and "mallory" in adapter.sent[0][1]
    await adapter.deliver("hi", user="mallory", chat_type="group")
    await adapter.wait_idle()
    assert len(adapter.sent) == 1


async def test_pairing_grants_an_unknown_dm_after_admin_approval(tmp_path):
    adapter = FakeAdapter()
    feishu = FeishuConfig(
        "app", "secret", allowed_users=frozenset({"alice"}), admins=frozenset({"alice"})
    )
    gateway = GatewayRunner(
        GatewayConfig(
            feishu=feishu,
            unauthorized_dm_behavior="pair",
            reset_policy=SessionResetPolicy(),
        ),
        options(tmp_path),
        adapter,
        provider_factory=ReplyProvider,
    )
    await gateway.start()
    try:
        await adapter.deliver("hello", user="mallory")
        await adapter.wait_idle()
        assert "配对码" in adapter.sent[-1][1]
        pending = gateway.pairing.list_pending("fake")
        assert len(pending) == 1 and pending[0]["user_id"] == "mallory"
        code = next(
            part.strip("`") for part in adapter.sent[-1][1].split() if len(part.strip("`")) == 8
        )
        await adapter.deliver(f"/pair approve {code}", user="alice")
        await adapter.wait_idle()
        assert "已批准用户 mallory" in adapter.sent[-1][1]
        await adapter.deliver("hello again", user="mallory")
        await adapter.wait_idle()
        assert adapter.sent[-1][1] == "reply: hello again"
    finally:
        await gateway.stop()


async def test_stop_cancels_pending_messages_and_accepts_new_input(runner):
    gateway, adapter, holder = runner
    blocking = BlockingProvider()
    holder["provider"] = blocking
    await adapter.deliver("slow one")
    await asyncio.wait_for(blocking.started.wait(), 10)
    assert adapter.is_busy("fake:dm:chat")
    await adapter.deliver("follow up")
    await adapter.deliver("/status")
    await eventually(lambda: any("执行中" in text for _, text, _ in adapter.sent))
    assert adapter.pending_count("fake:dm:chat") == 1
    await adapter.deliver("/stop")
    await eventually(lambda: any("正在停止" in text for _, text, _ in adapter.sent))
    blocking.release.set()
    await adapter.wait_idle()
    texts = [text for _, text, _ in adapter.sent]
    assert adapter.pending_count("fake:dm:chat") == 0
    assert any("排队消息已因停止取消" in text for text in texts)
    assert not any(text.endswith("follow up") for text in texts)
    assert not adapter.is_busy("fake:dm:chat")
    await adapter.deliver("after stop")
    await adapter.wait_idle()
    assert adapter.sent[-1][1] == "reply: after stop"


async def test_send_retries_and_idle_agents_are_closed(runner):
    gateway, adapter, _ = runner
    adapter.fail_sends = 1
    await adapter.deliver("hello")
    await adapter.wait_idle()
    assert [text for _, text, _ in adapter.sent] == ["reply: hello"]
    gateway.config = config(agent_idle_seconds=1.0)
    gateway._agents[gateway.store.get("fake:dm:chat").session_id].last_used -= 5
    assert await gateway.sweep_idle_agents() == 1
    assert gateway.cached_session_ids == ()


def test_split_message_prefers_paragraphs_and_closes_fences():
    text = "para one\n\n```python\nline\n" + "x" * 40 + "\n```\n\nlast"
    chunks = split_message(text, 40)
    assert "".join(chunks).count("para one") == 1
    # A reopened fence adds at most one fence line plus the closing fence.
    assert all(len(chunk) <= 40 + len("```python") + len("```") + 2 for chunk in chunks)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    assert split_message("short", 100) == ["short"]
    assert split_message("   ", 100) == []
