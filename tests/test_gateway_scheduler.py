from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from run_agent_gateway import (
    AgentGateway,
    CodingSessionTurnRunner,
    GatewayExtensionError,
    GatewayExtensionHost,
    InboundMessage,
    QueueGatewayAdapter,
    TurnRequest,
    TurnResult,
    TurnScheduler,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.active_by_session: defaultdict[str, int] = defaultdict(int)
        self.max_by_session: defaultdict[str, int] = defaultdict(int)
        self.total_active = 0
        self.max_total = 0
        self.started: list[str] = []
        self.release: dict[str, asyncio.Event] = {}

    async def run(self, request: TurnRequest, cancellation: asyncio.Event) -> TurnResult:
        self.started.append(request.id)
        self.active_by_session[request.session_id] += 1
        self.max_by_session[request.session_id] = max(
            self.max_by_session[request.session_id],
            self.active_by_session[request.session_id],
        )
        self.total_active += 1
        self.max_total = max(self.max_total, self.total_active)
        try:
            release = self.release.get(request.id)
            if release is not None:
                release_task = asyncio.create_task(release.wait())
                cancel_task = asyncio.create_task(cancellation.wait())
                done, pending = await asyncio.wait(
                    {release_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if cancel_task in done:
                    return TurnResult.cancelled(request)
            else:
                await asyncio.sleep(0.02)
            return TurnResult.succeeded(request, output=request.content.upper())
        finally:
            self.total_active -= 1
            self.active_by_session[request.session_id] -= 1


@pytest.mark.anyio
async def test_scheduler_serializes_same_session_in_submission_order() -> None:
    runner = RecordingRunner()
    runner.release["first"] = asyncio.Event()
    scheduler = TurnScheduler(runner, foreground_limit=4, background_limit=1)

    first = scheduler.submit(TurnRequest(id="first", session_id="same", content="one"))
    second = scheduler.submit(TurnRequest(id="second", session_id="same", content="two"))
    await asyncio.sleep(0.03)

    assert runner.started == ["first"]
    runner.release["first"].set()
    first_result, second_result = await asyncio.gather(first.result(), second.result())

    assert first_result.output == "ONE"
    assert second_result.output == "TWO"
    assert runner.max_by_session["same"] == 1
    await scheduler.shutdown()


@pytest.mark.anyio
async def test_scheduler_runs_different_sessions_concurrently() -> None:
    runner = RecordingRunner()
    runner.release["one"] = asyncio.Event()
    runner.release["two"] = asyncio.Event()
    scheduler = TurnScheduler(runner, foreground_limit=2, background_limit=1)

    one = scheduler.submit(TurnRequest(id="one", session_id="a", content="one"))
    two = scheduler.submit(TurnRequest(id="two", session_id="b", content="two"))
    await asyncio.sleep(0.03)

    assert set(runner.started) == {"one", "two"}
    assert runner.max_total == 2
    runner.release["one"].set()
    runner.release["two"].set()
    await asyncio.gather(one.result(), two.result())
    await scheduler.shutdown()


@pytest.mark.anyio
async def test_background_saturation_does_not_consume_foreground_capacity() -> None:
    runner = RecordingRunner()
    runner.release["background"] = asyncio.Event()
    scheduler = TurnScheduler(runner, foreground_limit=1, background_limit=1)

    background = scheduler.submit(
        TurnRequest(
            id="background",
            session_id="bg",
            content="slow",
            lane="background",
        )
    )
    await asyncio.sleep(0.02)
    foreground = scheduler.submit(TurnRequest(id="foreground", session_id="fg", content="fast"))
    foreground_result = await asyncio.wait_for(foreground.result(), timeout=0.5)

    assert foreground_result.status == "succeeded"
    assert runner.max_total == 2
    runner.release["background"].set()
    await background.result()
    await scheduler.shutdown()


@pytest.mark.anyio
async def test_cancelling_running_and_queued_turns_converges() -> None:
    runner = RecordingRunner()
    runner.release["running"] = asyncio.Event()
    scheduler = TurnScheduler(runner, foreground_limit=1, background_limit=1)
    running = scheduler.submit(TurnRequest(id="running", session_id="same", content="one"))
    queued = scheduler.submit(TurnRequest(id="queued", session_id="same", content="two"))
    await asyncio.sleep(0.03)

    queued.cancel()
    running.cancel()
    running_result, queued_result = await asyncio.gather(running.result(), queued.result())

    assert running_result.status == "cancelled"
    assert queued_result.status == "cancelled"
    assert "queued" not in runner.started
    assert scheduler.active_count == 0
    await scheduler.shutdown()


@pytest.mark.anyio
async def test_coding_session_turn_runner_uses_public_prompt_stream() -> None:
    from run_agent_core import AssistantMessage, MessageEndEvent

    class FakeSession:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.cancelled = False

        async def prompt(self, content: str):  # noqa: ANN201
            self.prompts.append(content)
            yield MessageEndEvent(message=AssistantMessage(content="session output", model="fake"))

        def cancel(self) -> None:
            self.cancelled = True

    session = FakeSession()
    runner = CodingSessionTurnRunner(lambda session_id: session)  # type: ignore[arg-type,return-value]
    request = TurnRequest(id="coding", session_id="session-1", content="hello")

    result = await runner.run(request, asyncio.Event())

    assert result.status == "succeeded"
    assert result.output == "session output"
    assert result.metadata == {"model": "fake", "stop_reason": "stop"}
    assert session.prompts == ["hello"]


@pytest.mark.anyio
async def test_coding_session_turn_runner_starts_new_session_without_prompting() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.new_session_count = 0

        async def new_session(self) -> str:
            self.new_session_count += 1
            return "Started new session: replacement"

        async def prompt(self, content: str):  # noqa: ANN201
            raise AssertionError(f"Unexpected prompt: {content}")
            yield

        def cancel(self) -> None:
            raise AssertionError("New-session command should not be cancelled")

    session = FakeSession()
    runner = CodingSessionTurnRunner(lambda session_id: session)  # type: ignore[arg-type,return-value]
    request = TurnRequest(id="new", session_id="session-1", content="  /new  ")

    result = await runner.run(request, asyncio.Event())

    assert result.status == "succeeded"
    assert result.output == "已开始新对话。此前聊天上下文已归档，长期记忆不受影响。"
    assert result.metadata == {"command": "new"}
    assert session.new_session_count == 1


@pytest.mark.anyio
async def test_gateway_routes_channel_conversations_to_stable_sessions() -> None:
    runner = RecordingRunner()
    scheduler = TurnScheduler(runner, foreground_limit=2, background_limit=1)
    adapter = QueueGatewayAdapter("test")
    gateway = AgentGateway(scheduler, [adapter])
    await gateway.start()

    await adapter.receive_message(
        InboundMessage(channel="test", conversation_id="room-1", text="hello")
    )
    outbound = await asyncio.wait_for(adapter.next_sent(), timeout=0.5)

    assert outbound.conversation_id == "room-1"
    assert outbound.text == "HELLO"
    assert runner.started
    await gateway.shutdown()
    assert scheduler.closed is True


def test_gateway_extension_host_loads_adapter_and_rolls_back_failed_setup(tmp_path) -> None:  # noqa: ANN001
    valid = tmp_path / "valid.py"
    valid.write_text(
        "from run_agent_gateway import QueueGatewayAdapter\n\n"
        "GATEWAY_EXTENSION_NAME = 'queue'\n\n"
        "def setup_gateway(api):\n"
        "    api.register_adapter(QueueGatewayAdapter('queue'))\n",
        encoding="utf-8",
    )
    duplicate = tmp_path / "duplicate.py"
    duplicate.write_text(
        "from run_agent_gateway import QueueGatewayAdapter\n\n"
        "def setup_gateway(api):\n"
        "    api.register_adapter(QueueGatewayAdapter('temporary'))\n"
        "    api.register_adapter(QueueGatewayAdapter('queue'))\n",
        encoding="utf-8",
    )
    host = GatewayExtensionHost()

    assert [adapter.name for adapter in host.load((valid,))] == ["queue"]
    with pytest.raises(GatewayExtensionError, match="already registered"):
        host.load((duplicate,))

    assert [adapter.name for adapter in host.adapters] == ["queue"]


@pytest.mark.anyio
async def test_gateway_preserves_message_id_and_reports_scheduler_rejection() -> None:
    runner = RecordingRunner()
    scheduler = TurnScheduler(runner, foreground_limit=1, background_limit=1, max_queued=2)
    adapter = QueueGatewayAdapter("test")
    gateway = AgentGateway(scheduler, [adapter])
    await gateway.start()

    await adapter.receive_message(
        InboundMessage(id="external-message", channel="test", conversation_id="room", text="ok")
    )
    outbound = await asyncio.wait_for(adapter.next_sent(), timeout=0.5)

    assert outbound.request_id == "external-message"
    assert scheduler.accepted_count == 1
    assert scheduler.completed_count == 1
    await gateway.shutdown()
