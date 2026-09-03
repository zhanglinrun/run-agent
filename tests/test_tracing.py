from __future__ import annotations

from pathlib import Path

import pytest

from agents.runtime.contracts import EventType
from agents.harness import (
    AgentHarness,
    BudgetSpec,
    ExtensionSettings,
    ProviderSettings,
    RuntimeConfig,
    SessionSettings,
    TaskSpec,
)
from agents.providers.base import ModelResponse
from agents.runtime.tracing import TraceRecorder, load_trace


def test_trace_is_append_only_and_redacts_secrets(tmp_path: Path) -> None:
    recorder = TraceRecorder(session_id="s1", model="m1", root=tmp_path)
    run_id = recorder.start_run("OPENAI_API_KEY=sk-abcdefgh12345678", api_key="secret")
    recorder.emit(EventType.MODEL_REQUEST, authorization="Bearer secret", input_tokens=10)
    recorder.finish_run(answer="done", tokens={"input": 10, "output": 2})

    events = load_trace(tmp_path / f"{run_id}.jsonl")
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["payload"]["api_key"] == "[REDACTED]"
    assert events[0]["payload"]["prompt"] == "OPENAI_API_KEY=[REDACTED]"
    assert events[2]["payload"]["authorization"] == "[REDACTED]"
    assert events[-1]["type"] == "run.completed"


@pytest.mark.asyncio
async def test_agent_run_writes_replayable_trace(tmp_path: Path) -> None:
    class Provider:
        async def complete(self, request):
            return ModelResponse(text="done", stop_reason="stop", usage={"input": 4, "output": 1})

    output = await AgentHarness().run(TaskSpec(
        "trace-run",
        "hello",
        tmp_path,
        mode="interactive",
        budget=BudgetSpec(total_turns=1, solve_turns=1, repair_turns=0, max_repair_attempts=0),
        runtime=RuntimeConfig(
            provider=ProviderSettings(adapter=Provider()),
            session=SessionSettings(trace_root=tmp_path / "traces"),
            extensions=ExtensionSettings(
                disabled=frozenset(
                    {
                        "plan",
                        "context",
                        "memory",
                        "subagents",
                        "skills",
                        "skill-evolution",
                        "mcp",
                        "verification",
                        "correction",
                        "acceptance",
                    }
                )
            ),
        ),
    ))
    events = load_trace(output.trace_path)

    assert output.answer == "done"
    assert [event["type"] for event in events] == [
        "run.started",
        "user.message",
        "model.request",
        "model.response",
        "run.completed",
    ]
