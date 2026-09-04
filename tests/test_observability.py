from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest

from pi_event_helpers import assistant_done, assistant_start, tool_call_end
from run_agent_ai.http import create_async_client
from run_agent_core import (
    AgentTool,
    AgentToolResult,
    AssistantMessage,
    TextContent,
    ToolCall,
    Usage,
    UsageCost,
    UserMessage,
)
from run_agent_core.loop import run_agent_loop
from run_agent_core.provider_events import AssistantMessageEvent
from run_agent_core.types import JSONValue
from run_agent_observability import (
    ProviderCallLedger,
    TraceRecorder,
    summarize_provider_calls,
    summarize_spans,
)


@pytest.mark.anyio
async def test_call_ledger_correlates_physical_http_attempts_and_usage(tmp_path) -> None:  # noqa: ANN001
    responses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(responses)
        return httpx.Response(
            status,
            request=request,
            json={"ok": status == 200},
            headers={"X-Cache": "MISS", "Content-Length": "12"},
        )

    class RetryingProvider:
        def __init__(self) -> None:
            self.client = create_async_client(transport=httpx.MockTransport(handler))

        def stream_response(self, **kwargs) -> AsyncIterator[AssistantMessageEvent]:  # noqa: ANN003
            async def iterator() -> AsyncIterator[AssistantMessageEvent]:
                kwargs.clear()
                first = await self.client.get("https://provider.test/v1/chat?api_key=secret")
                assert first.status_code == 503
                second = await self.client.get("https://provider.test/v1/chat?api_key=secret")
                assert second.status_code == 200
                message = AssistantMessage(
                    content="done",
                    model="fake",
                    usage=Usage(
                        input=10,
                        output=4,
                        cache_read=3,
                        cost=UsageCost(input=0.01, output=0.02, total=0.03),
                    ),
                )
                yield assistant_start(model="fake")
                yield assistant_done(message)

            return iterator()

    provider = RetryingProvider()
    ledger = ProviderCallLedger(tmp_path / "calls.jsonl", fsync=False)
    instrumented = ledger.instrument(provider, provider_name="test")
    events = [
        event
        async for event in instrumented.stream_response(
            model="fake",
            system="system",
            messages=[UserMessage(content="hello")],
            tools=[],
            session_id="session-1",
        )
    ]
    del events
    await provider.client.aclose()
    ledger.close()

    records = ledger.read_all()
    attempts = [record for record in records if record["type"] == "http_attempt"]
    summary = next(record for record in records if record["type"] == "provider_call")
    assert [record["status_code"] for record in attempts] == [503, 200]
    assert all(record["url"] == "https://provider.test/v1/chat" for record in attempts)
    assert len({record["logical_call_id"] for record in attempts}) == 1
    assert summary["physical_attempts"] == 2
    assert summary["retry_count"] == 1
    assert summary["cache_read_tokens"] == 3
    assert summary["cost"] == pytest.approx(0.03)
    efficiency = summarize_provider_calls(records)
    assert efficiency.logical_calls == 1
    assert efficiency.successful_calls == 1
    assert efficiency.physical_attempts == 2
    assert efficiency.retry_count == 1
    assert efficiency.cache_write_1h_tokens == 0
    assert efficiency.attempts_per_logical_call == 2.0
    assert efficiency.cost_per_successful_call == pytest.approx(0.03)


@pytest.mark.anyio
async def test_concurrent_call_ledgers_only_capture_their_own_http_attempts(tmp_path) -> None:  # noqa: ANN001
    release = asyncio.Event()
    both_started = asyncio.Event()
    started = 0

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.client = create_async_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, request=request, json={"ok": True})
                )
            )

        def stream_response(self, **kwargs) -> AsyncIterator[AssistantMessageEvent]:  # noqa: ANN003
            async def iterator() -> AsyncIterator[AssistantMessageEvent]:
                nonlocal started
                kwargs.clear()
                started += 1
                if started == 2:
                    both_started.set()
                await both_started.wait()
                await self.client.get(f"https://{self.name}.test/v1/chat")
                await release.wait()
                message = AssistantMessage(content=self.name, model="fake")
                yield assistant_start(model="fake")
                yield assistant_done(message)

            return iterator()

    providers = [Provider("alpha"), Provider("beta")]
    ledgers = [
        ProviderCallLedger(tmp_path / "alpha.jsonl", fsync=False),
        ProviderCallLedger(tmp_path / "beta.jsonl", fsync=False),
    ]
    instrumented = [
        ledgers[index].instrument(provider, provider_name=provider.name)
        for index, provider in enumerate(providers)
    ]

    async def consume(index: int) -> None:
        async for _event in instrumented[index].stream_response(
            model="fake",
            system="system",
            messages=[UserMessage(content="hello")],
            tools=[],
            session_id=providers[index].name,
        ):
            pass

    tasks = [asyncio.create_task(consume(index)) for index in range(2)]
    await both_started.wait()
    release.set()
    await asyncio.gather(*tasks)
    for provider in providers:
        await provider.client.aclose()
    for ledger in ledgers:
        ledger.close()

    for expected, ledger in zip(("alpha", "beta"), ledgers, strict=True):
        attempts = [row for row in ledger.read_all() if row["type"] == "http_attempt"]
        calls = [row for row in ledger.read_all() if row["type"] == "provider_call"]
        assert len(attempts) == 1
        assert attempts[0]["provider"] == expected
        assert attempts[0]["session_id"] == expected
        assert len(calls) == 1
        assert calls[0]["physical_attempts"] == 1


@pytest.mark.anyio
async def test_trace_recorder_builds_provider_tool_turn_and_agent_spans(tmp_path) -> None:  # noqa: ANN001
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content=[TextContent(text="ok")])

    call = ToolCall(id="call-1", name="read", arguments={})
    first = AssistantMessage(content=[call], model="fake")
    final = AssistantMessage(content="done", model="fake")

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        def stream_response(self, **kwargs) -> AsyncIterator[AssistantMessageEvent]:  # noqa: ANN003
            del kwargs

            async def iterator() -> AsyncIterator[AssistantMessageEvent]:
                self.calls += 1
                if self.calls == 1:
                    yield assistant_start()
                    yield tool_call_end(call)
                    yield assistant_done(first, "toolUse")
                else:
                    yield assistant_start()
                    yield assistant_done(final)

            return iterator()

    recorder = TraceRecorder(tmp_path / "trace.jsonl", session_id="session-1", fsync=False)
    stream = run_agent_loop(
        provider=Provider(),
        model="fake",
        system="system",
        messages=[UserMessage(content="run")],
        tools=[
            AgentTool(
                name="read",
                label="Read",
                description="read",
                parameters={"type": "object"},
                execute_fn=execute,
            )
        ],
    )
    async for event in stream:
        await recorder(event)

    records = recorder.read_all()
    names = [record["name"] for record in records]
    assert names.count("provider") == 2
    assert names.count("turn") == 2
    assert names.count("tool:read") == 1
    assert names.count("agent") == 1
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    summary = summarize_spans(records)
    assert summary["provider"]["count"] == 2
    assert summary["agent"]["max_ms"] >= 0
