import asyncio
import sqlite3
from dataclasses import replace

import httpx
import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_ai.http import ObservedAsyncClient
from run_agent_ai.model_limits import RuntimeModelLimits
from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionError
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.telemetry import SqliteTelemetrySink, TelemetryUnavailable
from run_agent_core.messages import AssistantMessage, TextContent, Usage, UsageCost
from run_agent_core.provider_events import AssistantErrorEvent, TextDeltaEvent
from run_agent_observability import ProviderCallLedger, summarize_provider_calls
from run_agent_observability.sink import ScopedTelemetrySink, read_stream


async def test_sqlite_telemetry_freezes_batches_pages_and_records_loss(tmp_path):
    path = tmp_path / "state.sqlite3"
    async with await SqliteDatabase.open(path) as database:
        sink = SqliteTelemetrySink(database, max_pending=1010)
        original = {"value": ["before"]}
        assert sink.emit("spans", original)
        original["value"].append("after")
        for number in range(1005):
            assert sink.emit("spans", {"index": number})
        assert sink.emit("private", {"not_in_spans": True})
        assert not sink.emit("spans", {"oversized": "x" * 70000})
        await sink.aclose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1007
        assert connection.execute("SELECT dropped,failed FROM observation_health").fetchone() == (
            1,
            0,
        )
    async with await SqliteDatabase.open(path) as reopened:
        sink = SqliteTelemetrySink(reopened)
        rows = await read_stream(sink, "spans")
        assert len(rows) == 1006
        assert rows[0]["value"] == ["before"]
        assert rows[-1]["index"] == 1004
        await sink.aclose()
    assert not list(tmp_path.rglob("*.jsonl"))


async def test_durable_accounting_waits_even_when_optional_queue_is_full(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database, max_pending=1)
        assert sink.emit("trace", {"n": 1})
        assert not sink.emit("trace", {"n": 2})
        await sink.append("ledger", {"cost": 0.5})
        assert (await sink.read("ledger"))[0]["cost"] == 0.5
        await sink.aclose()


async def test_writer_failure_exposes_loss_and_close_does_not_hang(tmp_path, monkeypatch):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database, batch_size=1)

        def fail(connection, *, batch):
            raise OSError("simulated full disk")

        monkeypatch.setattr("run_agent_coding.storage.telemetry._write_batch", fail)
        assert sink.emit("trace", {"n": 1})
        assert sink.emit("trace", {"n": 2})
        with pytest.raises(TelemetryUnavailable, match="persistence failed"):
            await asyncio.wait_for(sink.flush(), 2)
        assert sink.failed == 2
        assert not sink.emit("trace", {"n": 3})
        with pytest.raises(TelemetryUnavailable):
            await sink.append("ledger", {"cost": 1})
        with pytest.raises(TelemetryUnavailable):
            await asyncio.wait_for(sink.aclose(), 2)
        assert sink._writer.done()


async def test_scoped_telemetry_cannot_read_another_extension_or_write_after_retirement(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database)
        active = True

        def check():
            if not active:
                raise RuntimeError("retired generation")

        first = ScopedTelemetrySink(sink, "source-a:", check)
        second = ScopedTelemetrySink(sink, "source-b:", lambda: None)
        await first.append("trace", {"source": "a"})
        await second.append("trace", {"source": "b"})
        assert [row["source"] for row in await first.read("trace")] == ["a"]
        active = False
        with pytest.raises(RuntimeError, match="retired"):
            first.emit("trace", {"late": True})
        with pytest.raises(RuntimeError, match="retired"):
            await first.append("trace", {"late": True})
        await sink.aclose()


class RetryingProvider:
    async def stream_response(self, **kwargs):
        attempt = 0

        def response(request):
            nonlocal attempt
            attempt += 1
            return httpx.Response(503 if attempt == 1 else 200)

        async with ObservedAsyncClient(transport=httpx.MockTransport(response)) as client:
            await client.get("https://example.invalid/v1?api_key=secret")
            await client.get("https://example.invalid/v1?api_key=secret")
        yield AssistantErrorEvent(
            reason="error",
            error=AssistantMessage(
                model="test",
                provider="test",
                stop_reason="error",
                error_message="failed",
                usage=Usage(input=4, output=3, total_tokens=7, cost=UsageCost(total=0.7)),
            ),
        )


async def test_failed_physical_attempts_and_reported_cost_are_durable(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database, max_pending=1)
        ledger = ProviderCallLedger(sink, stream="calls")
        provider = ledger.instrument(RetryingProvider(), provider_name="test")
        try:
            for _ in range(10):
                sink.emit("trace", {"n": 0})
            events = [
                event
                async for event in provider.stream_response(
                    model="test",
                    system="",
                    messages=[],
                    tools=[],
                    session_id="owned",
                )
            ]
            assert events[-1].type == "error"
            records = await ledger.read_all()
            summary = summarize_provider_calls(records)
            assert summary.failed_calls == 1
            assert summary.physical_attempts == 2
            assert summary.retry_count == 1
            assert summary.total_cost == 0.7
            assert ledger.complete
            attempts = [record for record in records if record["type"] == "http_attempt"]
            assert [record["status_code"] for record in attempts] == [503, 200]
            assert "secret" not in str(records)
        finally:
            ledger.close()
            await sink.aclose()


async def test_failed_final_ledger_write_preserves_attempts_and_marks_accounting_incomplete(
    tmp_path,
    monkeypatch,
):
    import run_agent_coding.storage.telemetry as storage_telemetry

    original = storage_telemetry._write_batch

    def fail_final(connection, *, batch):
        if any('"type":"provider_call"' in body for _, body in batch):
            raise OSError("final accounting write failed")
        original(connection, batch=batch)

    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database)
        ledger = ProviderCallLedger(sink, stream="calls")
        monkeypatch.setattr(storage_telemetry, "_write_batch", fail_final)
        provider = ledger.instrument(RetryingProvider(), provider_name="test")
        events = [
            event
            async for event in provider.stream_response(
                model="test",
                system="",
                messages=[],
                tools=[],
            )
        ]
        assert events[-1].type == "error"
        rows = await ledger.read_all()
        summary = summarize_provider_calls(rows)
        assert summary.logical_calls == 1
        assert summary.physical_attempts == 2
        assert ledger.complete is False
        ledger.close()
        with pytest.raises(TelemetryUnavailable):
            await sink.aclose()


class PartialProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream_response(self, **kwargs):
        try:
            yield TextDeltaEvent(
                content_index=0,
                delta="partial",
                partial=AssistantMessage(
                    content=[TextContent(text="partial")],
                    model="test",
                    provider="test",
                    usage=Usage(input=5, output=2, total_tokens=7, cost=UsageCost(total=0.2)),
                ),
            )
            self.started.set()
            await asyncio.Event().wait()
        finally:
            self.closed.set()


async def test_cancelled_stream_closes_upstream_and_commits_partial_usage(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sink = SqliteTelemetrySink(database)
        ledger = ProviderCallLedger(sink, stream="cancelled")
        raw = PartialProvider()
        provider = ledger.instrument(raw, provider_name="test")

        async def consume():
            return [
                event
                async for event in provider.stream_response(
                    model="test",
                    system="",
                    messages=[],
                    tools=[],
                )
            ]

        operation = asyncio.create_task(consume())
        await raw.started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert raw.closed.is_set()
        records = await ledger.read_all()
        record = next(row for row in records if row["type"] == "provider_call")
        assert record["status"] == "cancelled"
        assert record["cost"] == 0.2
        assert ledger.complete
        ledger.close()
        await sink.aclose()


async def test_application_name_compaction_reload_and_limits_remain_instrumented(tmp_path):
    class LimitedProvider(ReplyProvider):
        async def discover_model_limits(self, model):
            return RuntimeModelLimits(context_window=100000)

    manager = SessionManager(options(tmp_path).paths)
    ledger = ProviderCallLedger(await manager.telemetry(), stream="calls")
    raw = LimitedProvider()
    # Tracing is a session feature now; an empty extension keeps a scoped sink reachable.
    extension = tmp_path / "scoped.py"
    extension.write_text("def setup(api): pass", encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,), trace_enabled=True)
    try:
        async with await CodingApplication.open(
            opts,
            manager=manager,
            provider=raw,
            provider_transform=lambda provider, name: ledger.instrument(
                provider, provider_name=name
            ),
        ) as app:
            assert await app.session.provider.discover_model_limits("test") == RuntimeModelLimits(
                context_window=100000,
            )
            events = [event async for event in app.prompt("hello")]
            assert events[-1].status == "succeeded"
            runtime = app.session.extension_runtime
            old_sink = runtime._fresh_context(runtime._extensions[0].source_id).telemetry
            await app.command("/compact")
            await app.command("/reload")
            with pytest.raises(ExtensionError):
                old_sink.emit("trace", {"late": True})
            events = [event async for event in app.prompt("again")]
            assert events[-1].status == "succeeded"
            result = await app.command("/trace")
            assert "sqlite" in result.message
            assert app.session.trace_recorder is not None
            assert app.session.trace_recorder.span_count > 0
        rows = await ledger.read_all()
        # First answer, automatic name, manual compaction, second answer.
        assert sum(row["type"] == "provider_call" for row in rows) == 4
        assert ledger.complete
    finally:
        ledger.close()
        await manager.aclose()
