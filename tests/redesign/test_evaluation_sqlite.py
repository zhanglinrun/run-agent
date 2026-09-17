import asyncio
import json
import sys
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import options

from run_agent_coding.application import CodingApplication
from run_agent_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from run_agent_coding.session import ModelChoice
from run_agent_coding.session_manager import SessionManager
from run_agent_core.messages import AssistantMessage, TextContent, Usage, UsageCost
from run_agent_core.provider_events import AssistantDoneEvent, AssistantErrorEvent, TextDeltaEvent
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import ExecutionCancelled, FrozenTask
from run_agent_evals.runner import EvaluationRunner, reduce_trials
from run_agent_evals.runtime_bench import RuntimeBenchmarkConfig, _trace_samples
from run_agent_observability import ProviderCallLedger


def settings():
    return ProviderSettings(
        default_provider="test-a",
        providers=tuple(
            OpenAICompatibleProviderConfig(
                name=name,
                models=("first", "second"),
                default_model="first",
                api_key_env="TELEMETRY_TEST_API_KEY",
            )
            for name in ("test-a", "test-b")
        ),
    )


class EvaluatedProvider:
    def __init__(self, *, mode="success"):
        self.mode = mode
        self.started = asyncio.Event()
        self.closed = False

    async def stream_response(self, *, system, model, **kwargs):
        naming = "session name" in system.lower() or "session title" in system.lower()
        usage = Usage(input=10, output=5, total_tokens=15, cost=UsageCost(total=0.25))
        message = AssistantMessage(
            content=[TextContent(text="offline answer")],
            usage=usage,
            model=model,
            provider="test-a",
            stop_reason="stop",
        )
        if self.mode == "cancel" and not naming:
            yield TextDeltaEvent(content_index=0, delta="partial", partial=message)
            self.started.set()
            await asyncio.Event().wait()
        if self.mode == "error" and not naming:
            yield AssistantErrorEvent(
                reason="error",
                error=message.model_copy(
                    update={
                        "stop_reason": "error",
                        "error_message": "injected model error",
                    }
                ),
            )
        else:
            yield AssistantDoneEvent(reason="stop", message=message)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["success", "error"])
async def test_eval_uses_real_application_and_failure_keeps_cost(tmp_path, monkeypatch, mode):
    providers = []

    def create(*args, **kwargs):
        provider = EvaluatedProvider(mode=mode)
        providers.append(provider)
        return provider

    monkeypatch.setattr("run_agent_coding.session.create_model_provider", create)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = FrozenTask("case", fixture, "answer", ((sys.executable, "-c", "pass"),))
    executor = CodingTaskExecutor(tmp_path / "state", provider_settings=settings())
    trial = await EvaluationRunner(tmp_path / "results").run_trial(
        task,
        executor,
        candidate_id="baseline",
        seed=0,
    )
    assert trial.status == ("passed" if mode == "success" else "error")
    assert trial.metadata["accounting_complete"] is True
    assert trial.metadata["cost"] > 0
    assert trial.metadata["calls"] >= 1
    assert reduce_trials([trial]).total_cost == trial.metadata["cost"]
    observations = tmp_path / "state" / "logs" / "observations.jsonl"
    assert observations.is_file()
    body = observations.read_text(encoding="utf-8")
    assert trial.metadata["call_stream"] in body
    assert trial.metadata["eval_input"].endswith("eval-input.json")
    assert all(provider.closed for provider in providers)


async def test_cancelled_eval_archives_durable_partial_cost(tmp_path, monkeypatch):
    provider = EvaluatedProvider(mode="cancel")
    monkeypatch.setattr("run_agent_coding.session.create_model_provider", lambda *a, **kw: provider)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    task = FrozenTask("cancel", fixture, "work", ())
    executor = CodingTaskExecutor(tmp_path / "state", provider_settings=settings())
    operation = asyncio.create_task(
        EvaluationRunner(tmp_path / "results").run_trial(
            task,
            executor,
            candidate_id="baseline",
            seed=0,
        )
    )
    await asyncio.wait_for(provider.started.wait(), 3)
    operation.cancel()
    with pytest.raises(ExecutionCancelled):
        await operation
    evidence = json.loads(next((tmp_path / "results/trials").glob("*.json")).read_text())
    assert evidence["status"] == "cancelled"
    assert evidence["metadata"]["known_cost"] >= 0.25
    assert evidence["metadata"]["accounting_complete"] is True
    assert provider.closed


async def test_switching_provider_preserves_ledger_and_closes_old_instance(tmp_path, monkeypatch):
    providers = []

    def create(*args, **kwargs):
        provider = EvaluatedProvider()
        providers.append(provider)
        return provider

    monkeypatch.setattr("run_agent_coding.session.create_model_provider", create)
    monkeypatch.setenv("TELEMETRY_TEST_API_KEY", "offline")
    manager = SessionManager(options(tmp_path).paths)
    ledger = ProviderCallLedger(await manager.telemetry(), stream="switches")
    try:
        async with await CodingApplication.open(
            replace(options(tmp_path), provider_name="test-a", model="first"),
            settings=settings(),
            manager=manager,
            provider_transform=lambda provider, name: ledger.instrument(
                provider, provider_name=name
            ),
        ) as app:
            assert [event async for event in app.prompt("first")][-1].status == "succeeded"
            await app.session.select_provider_model(
                ModelChoice(provider_name="test-b", model="second")
            )
            assert providers[0].closed
            assert [event async for event in app.prompt("second")][-1].status == "succeeded"
        calls = [row for row in await ledger.read_all() if row["type"] == "provider_call"]
        assert {row["provider"] for row in calls} == {"test-a", "test-b"}
        assert {row["model"] for row in calls} == {"first", "second"}
        assert all(provider.closed for provider in providers)
    finally:
        ledger.close()
        await manager.aclose()


async def test_trace_benchmark_archives_jsonl_and_durable_timings(tmp_path):
    samples, artifacts = await _trace_samples(
        tmp_path,
        RuntimeBenchmarkConfig(trace_repeats=2, tool_calls=2),
    )
    assert samples["durable_flush_included"] is True
    assert len(samples["traced_duration_ms"]) == 2
    assert all(count > 0 for count in samples["span_counts"])
    assert samples["database_bytes"] == artifacts[0].stat().st_size
    lines = [line for line in artifacts[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == sum(samples["span_counts"])
