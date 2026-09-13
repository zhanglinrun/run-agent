"""The Feishu adapter normalizes SDK messages and sends Markdown replies."""

import asyncio
from types import SimpleNamespace

import pytest

from run_agent_gateway.config import FeishuConfig
from run_agent_gateway.platforms.feishu import FeishuAdapter
from run_agent_gateway.platforms.feishu_lock import AppInstanceLock

pytest.importorskip("lark_oapi.channel")


class Channel:
    def __init__(self):
        self.sent = []
        self.reactions = []
        self.removed = []
        self.stopped = False
        self.callback = None
        self.fail_once = False

    def on(self, name, callback):
        assert name == "message"
        self.callback = callback
        return lambda: setattr(self, "callback", None)

    async def start_background(self):
        return None

    def stop(self):
        self.stopped = True

    async def send(self, to, message, opts):
        self.sent.append((to, message, opts))
        if self.fail_once:
            self.fail_once = False
            return SimpleNamespace(success=False, error="lost", message_id=None)
        return SimpleNamespace(success=True, message_id=f"om{len(self.sent)}")

    async def add_reaction(self, message_id, emoji):
        self.reactions.append((message_id, emoji))
        return SimpleNamespace(success=True, raw={"data": {"reaction_id": "r1"}})

    async def remove_reaction(self, message_id, reaction_id):
        self.removed.append((message_id, reaction_id))
        return SimpleNamespace(success=True)


def message(text, *, chat_type="p2p", mentioned=False, message_id="m1", thread=None, bot=False):
    return SimpleNamespace(
        id=message_id,
        content_text=text,
        mentioned_bot=mentioned,
        conversation=SimpleNamespace(chat_id="chat", chat_type=chat_type, thread_id=thread),
        sender=SimpleNamespace(open_id="ou_alice", display_name="Alice", is_bot=bot),
        content=SimpleNamespace(kind="text"),
        raw_content_type="text",
    )


def adapter_with(channel, **overrides):
    config = FeishuConfig("app", "secret", allow_all_users=True, **overrides)
    return FeishuAdapter(config, channel=channel)


def test_app_lock_is_atomic_and_reusable_after_release(tmp_path):
    path = tmp_path / "feishu.lock"
    first = AppInstanceLock(path)
    second = AppInstanceLock(path)
    assert first.acquire()[0]
    acquired, existing = second.acquire()
    assert not acquired and existing is not None
    first.release()
    assert second.acquire()[0]
    second.release()

    adapter = adapter_with(Channel())
    dm = adapter.build_event(message("hello"))
    assert dm is not None and dm.source.chat_type == "dm" and dm.source.user_id == "ou_alice"
    assert dm.text == "hello" and dm.message_id == "m1"
    assert adapter.build_event(message("hello", message_id="m1")) is None  # duplicate
    assert adapter.build_event(message("hi group", chat_type="group", message_id="m2")) is None
    group = adapter.build_event(
        message("@_user_1 hi group", chat_type="group", mentioned=True, message_id="m3", thread="t")
    )
    assert group is not None and group.text == "hi group"
    assert group.source.chat_type == "group" and group.source.thread_id == "t"
    assert adapter.build_event(message("bot", message_id="m4", bot=True)) is None
    relaxed = adapter_with(Channel(), require_mention=False)
    assert relaxed.build_event(message("plain", chat_type="group", message_id="m5")) is not None


def test_app_lock_does_not_reclaim_an_initializing_file(tmp_path):
    path = tmp_path / "initializing.lock"
    path.write_text("", encoding="utf-8")
    lock = AppInstanceLock(path)
    acquired, existing = lock.acquire()
    assert not acquired and existing == {"state": "initializing"}
    path.unlink()


async def test_card_action_preserves_approval_origin_context():
    adapter = adapter_with(Channel())
    calls = []

    async def resolve(approval_id, choice, user_id, chat_id, thread_id):
        calls.append((approval_id, choice, user_id, chat_id, thread_id))

    adapter.set_approval_handler(resolve)
    action = SimpleNamespace(
        action=SimpleNamespace(value={"run_action": "approve_once", "approval_id": "aprandom"}),
        operator=SimpleNamespace(open_id="ou_bob"),
        context=SimpleNamespace(open_chat_id="chat-A", open_thread_id="thread-1"),
    )
    await adapter._on_card_action(action)
    assert calls == [("aprandom", "once", "ou_bob", "chat-A", "thread-1")]


async def test_inbound_messages_reach_the_handler_and_replies_are_chunked():
    channel = Channel()
    adapter = adapter_with(channel, max_message_length=200)
    received = []
    done = asyncio.Event()

    async def handler(event):
        received.append(event)
        done.set()
        return "line\n" * 60

    adapter.set_message_handler(handler)
    assert await adapter.connect()
    channel.callback(message("hello", thread="thread-1"))
    await asyncio.wait_for(done.wait(), 5)
    await adapter.wait_idle()
    assert received[0].text == "hello"
    assert len(channel.sent) >= 2
    first_to, first_body, first_opts = channel.sent[0]
    assert first_to == "chat" and "markdown" in first_body
    assert first_opts.reply_to == "m1" and first_opts.reply_in_thread is True
    assert channel.sent[1][2].reply_to is None  # reply_to_mode=first
    assert channel.reactions == [("m1", "Typing")] and channel.removed == [("m1", "r1")]
    await adapter.disconnect()
    assert channel.callback is None and not channel.stopped  # injected channel is not owned


async def test_send_reports_failures_for_the_retry_loop():
    channel = Channel()
    channel.fail_once = True
    adapter = adapter_with(channel)
    await adapter.connect()
    failed = await adapter.send("chat", "one")
    assert not failed.success and failed.error == "lost"
    ok = await adapter.send("chat", "two", reply_to="m9")
    assert ok.success and ok.message_id == "om2"
