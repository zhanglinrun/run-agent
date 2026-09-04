from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("lark_oapi.channel")

from examples.gateway_extensions.feishu import FeishuAdapter, _GatewayFeishuChannel
from lark_oapi.channel import FeishuChannel
from lark_oapi.ws import client as ws_client_module

from run_agent_gateway import OutboundMessage


class FakeFeishuChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.started = asyncio.Event()
        self.sent: list[tuple[str, object, object]] = []
        self.stopped = False

    def on(self, name: str, handler: Any):  # noqa: ANN201
        self.handlers[name] = handler
        return lambda: self.handlers.pop(name, None)

    async def start_background(self) -> None:
        self.started.set()

    async def send(self, to: str, message: object, opts: object):  # noqa: ANN201
        self.sent.append((to, message, opts))
        return SimpleNamespace(success=True, error=None)

    def stop(self) -> None:
        self.stopped = True

    def emit(self, message: object) -> None:
        self.handlers["message"](message)


def _adapter(channel: FakeFeishuChannel) -> FeishuAdapter:
    return FeishuAdapter(
        environment={
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret",
        },
        channel=channel,
    )


@pytest.mark.anyio
async def test_feishu_adapter_receives_and_replies_to_message() -> None:
    channel = FakeFeishuChannel()
    adapter = _adapter(channel)
    stream = adapter.messages()
    inbound_task = asyncio.create_task(anext(stream))
    await channel.started.wait()

    channel.emit(
        SimpleNamespace(
            id="om_123",
            content_text="check this repository",
            content=SimpleNamespace(kind="text"),
            raw_content_type="text",
            mentioned_bot=True,
            conversation=SimpleNamespace(
                chat_id="oc_123",
                chat_type="group",
                thread_id=None,
            ),
            sender=SimpleNamespace(open_id="ou_123", display_name="Developer"),
        )
    )
    inbound = await asyncio.wait_for(inbound_task, timeout=1)

    assert inbound.id == "om_123"
    assert inbound.channel == "feishu"
    assert inbound.conversation_id == "oc_123"
    assert inbound.text == "check this repository"
    assert inbound.metadata == {
        "chat_type": "group",
        "sender_id": "ou_123",
        "message_type": "text",
        "mentioned_bot": True,
        "sender_name": "Developer",
    }

    await adapter.send(
        OutboundMessage(
            channel="feishu",
            conversation_id="oc_123",
            text="done",
            request_id="om_123",
            status="succeeded",
        )
    )
    assert channel.sent[0][:2] == ("oc_123", {"markdown": "done"})
    assert channel.sent[0][2].reply_to == "om_123"

    next_message = asyncio.create_task(anext(stream))
    await adapter.close()
    with pytest.raises(StopAsyncIteration):
        await next_message
    assert channel.stopped is True
    assert channel.handlers == {}


def test_feishu_adapter_requires_credentials() -> None:
    with pytest.raises(ValueError, match="FEISHU_APP_ID"):
        FeishuAdapter(environment={})


def test_real_feishu_channel_binds_sdk_loop_to_start_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_ws_loop = ws_client_module.loop
    observed: dict[str, asyncio.AbstractEventLoop] = {}

    def fake_start(channel: FeishuChannel) -> None:
        del channel
        observed["thread"] = asyncio.get_event_loop()
        observed["sdk"] = ws_client_module.loop

    monkeypatch.setattr(FeishuChannel, "start", fake_start)
    channel = object.__new__(_GatewayFeishuChannel)

    channel.start()

    assert observed["thread"] is observed["sdk"]
    assert observed["thread"].is_closed()
    assert ws_client_module.loop is previous_ws_loop
