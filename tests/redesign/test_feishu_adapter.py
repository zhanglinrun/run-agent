import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_agent_gateway import Delivery


def adapter_type():
    pytest.importorskip("lark_oapi.channel")
    path = Path(__file__).resolve().parents[2] / "examples/gateway_extensions/feishu.py"
    spec = importlib.util.spec_from_file_location("redesign_feishu", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.GATEWAY_EXTENSION_API_VERSION == 2
    return module.FeishuAdapter


class Channel:
    def __init__(self):
        self.sent = []
        self.started = asyncio.Event()
        self.closed = False
        self.fail_chunk = True

    def on(self, name, callback):
        self.callback = callback
        return lambda: None

    async def start_background(self):
        self.started.set()

    def stop(self):
        self.closed = True

    async def send(self, destination, message, opts):
        self.sent.append((destination, message, opts))
        if len(self.sent) == 2 and self.fail_chunk:
            self.fail_chunk = False
            return SimpleNamespace(success=False, error="lost request", message_id=None)
        return SimpleNamespace(success=True, message_id=opts.uuid)


async def test_feishu_uses_verified_account_sender_thread_and_closes():
    channel = Channel()
    adapter = adapter_type()(
        environment={"FEISHU_APP_ID": "account", "FEISHU_APP_SECRET": "test"}, channel=channel
    )
    iterator = adapter.messages()
    receive = asyncio.create_task(anext(iterator))
    await channel.started.wait()
    channel.callback(
        SimpleNamespace(
            id="message",
            content_text="/status",
            conversation=SimpleNamespace(chat_id="chat", chat_type="group", thread_id="thread"),
            sender=SimpleNamespace(open_id="alice", is_bot=False),
            content=None,
        )
    )
    message = await asyncio.wait_for(receive, 2)
    assert (message.account_id, message.sender_id, message.thread_id) == (
        "account",
        "alice",
        "thread",
    )
    assert message.source_message_id == "message"
    await adapter.close()
    await iterator.aclose()
    assert channel.closed


async def test_feishu_chunk_retries_keep_each_uuid_and_original_destination():
    channel = Channel()
    adapter = adapter_type()(
        environment={"FEISHU_APP_ID": "account", "FEISHU_APP_SECRET": "test"}, channel=channel
    )
    delivery = Delivery(
        "delivery",
        "task",
        "result",
        {
            "account_id": "account",
            "chat_id": "chat",
            "thread_id": "thread",
            "source_message_id": "original-message",
        },
        {"output": "中文" * 1600},
        1,
    )
    with pytest.raises(RuntimeError, match="lost request"):
        await adapter.send(delivery)
    receipt = await adapter.send(delivery)
    assert len(receipt["message_ids"]) == 3
    assert channel.sent[0][2].uuid == channel.sent[2][2].uuid
    assert channel.sent[1][2].uuid == channel.sent[3][2].uuid
    assert len({item[2].uuid for item in channel.sent}) == 3
    assert all(
        item[0] == "chat" and item[2].reply_to == "original-message" and item[2].reply_in_thread
        for item in channel.sent
    )
    await adapter.close()
