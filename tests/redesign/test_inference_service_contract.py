"""The InferenceService host contract.

The background review has to reason about a run it did not take part in, so it cannot
borrow the foreground agent's turn to do it. The host therefore exposes one bounded
completion capability, and it has to stay honest about three things: whether a provider
is reachable at all, that the exact model input is committed before a physical provider
sees it, and that it refuses to run while a foreground run is in flight, because a
review that competes with the user's own turn is the failure the review gate exists to
prevent.

The service is deliberately narrower than the agent loop: no tools, no transcript, no
history. A caller gets text and usage back, and nothing it returns becomes session
history.
"""

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from tests.redesign.test_coding_application import options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import HostServices
from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceRequest,
    InferenceUnavailable,
    UnavailableInference,
)
from run_agent_core.messages import AssistantMessage, TextContent, ToolCall, Usage
from run_agent_core.provider_events import AssistantDoneEvent


class UsageProvider:
    """A provider that reports token usage, so the caller can attribute its spend."""

    def __init__(self, text: str = "reviewed") -> None:
        self.text = text
        self.calls = 0

    async def stream_response(self, *, model, system, messages, **kwargs):
        self.calls += 1
        yield AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                content=[TextContent(text=self.text)],
                model=model,
                provider="test",
                stop_reason="stop",
                usage=Usage(input=120, output=30, total_tokens=150),
            ),
        )


PROBE = """
import json
from pathlib import Path

from run_agent_coding.host.inference import InferenceRequest


def setup(api):
    async def observe(event, context):
        record = {"available": None, "text": None, "snapshot_id": None, "error": None}
        service = api.context.services.inference
        record["available"] = service.available
        try:
            result = await service.complete(
                InferenceRequest(prompt="probe", system="probe system", purpose="probe")
            )
            record["text"] = result.text
            record["snapshot_id"] = result.snapshot_id
        except Exception as exc:
            record["error"] = type(exc).__name__
        target = Path(api.context.paths.home) / "inference-probe.json"
        target.write_text(json.dumps(record), encoding="utf-8")

    api.on("turn_start", observe)
"""


@pytest.fixture
def probe(tmp_path):
    path = tmp_path / "probe.py"
    path.write_text(PROBE, encoding="utf-8")
    return path


def probe_options(tmp_path, probe):
    return replace(options(tmp_path), extension_paths=(probe,))


def test_host_services_exposes_inference_beside_the_other_services():
    for name in ("tasks", "snapshots", "history", "evaluation", "inference"):
        assert hasattr(HostServices, name), name


def test_an_inference_request_is_frozen_and_validated():
    request = InferenceRequest(prompt="what happened", system="be terse", purpose="review")
    assert (request.prompt, request.system, request.purpose) == (
        "what happened",
        "be terse",
        "review",
    )
    with pytest.raises(TypeError):
        InferenceRequest(prompt="what happened", tools=[])
    with pytest.raises((FrozenInstanceError, AttributeError)):
        request.prompt = "something else"
    with pytest.raises(ValueError):
        InferenceRequest(prompt="")
    with pytest.raises(ValueError):
        InferenceRequest(prompt="ok", purpose="x" * 129)


def test_unavailable_inference_reports_itself_unavailable():
    service = UnavailableInference()
    assert service.available is False
    with pytest.raises(InferenceUnavailable):
        import asyncio

        asyncio.run(service.complete(InferenceRequest(prompt="anything")))


async def test_a_session_with_a_provider_answers_one_bounded_completion(tmp_path, probe):
    provider = UsageProvider(text="a candidate worth proposing")
    async with await CodingApplication.open(
        probe_options(tmp_path, probe), provider=provider
    ) as app:
        await app.start()
        inference = context(app).services.inference
        assert inference.available is True

        result = await inference.complete(
            InferenceRequest(
                prompt="review this run", system="be terse", purpose="experience_review"
            )
        )

    assert result.text == "a candidate worth proposing"
    assert result.model == "test"
    assert (result.input_tokens, result.output_tokens) == (120, 30)
    assert result.snapshot_id


async def test_the_exact_input_is_committed_before_the_provider_runs(tmp_path, probe):
    provider = UsageProvider()
    async with await CodingApplication.open(
        probe_options(tmp_path, probe), provider=provider
    ) as app:
        await app.start()
        services = context(app).services
        result = await services.inference.complete(
            InferenceRequest(prompt="the evidence", system="the rules", purpose="experience_review")
        )

        recorded = await services.snapshots.read(result.snapshot_id)

    assert recorded.payload["purpose"] == "extension:experience_review"
    assert recorded.payload["system"] == "the rules"
    assert "the evidence" in json.dumps(recorded.payload["messages"])
    assert recorded.payload["tools"] == []


async def test_inference_refuses_while_a_foreground_run_is_in_flight(tmp_path, probe):
    provider = UsageProvider()
    async with await CodingApplication.open(
        probe_options(tmp_path, probe), provider=provider
    ) as app:
        await app.start()
        [event async for event in app.prompt("do some work")]
        record = json.loads(
            (tmp_path / "state" / "inference-probe.json").read_text(encoding="utf-8")
        )

    assert record["available"] is True
    assert record["error"] == InferenceBusy.__name__
    assert record["text"] is None


async def test_inference_returns_selected_native_calls_without_executing_them(tmp_path, probe):
    destination = tmp_path / "must-not-be-written.txt"

    class NativeProvider:
        async def stream_response(self, *, tools, **kwargs):
            assert [tool.name for tool in tools] == ["write"]
            yield AssistantDoneEvent(
                reason="stop",
                message=AssistantMessage(
                    content=[
                        ToolCall(
                            id="proposed-write",
                            name="write",
                            arguments={"path": str(destination), "content": "not executed"},
                        )
                    ]
                ),
            )

    async with await CodingApplication.open(
        probe_options(tmp_path, probe), provider=NativeProvider()
    ) as app:
        await app.start()
        inference = context(app).services.inference
        result = await inference.complete(InferenceRequest(prompt="propose", tool_names=("write",)))
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "write"
        assert not destination.exists()
        with pytest.raises(ValueError, match="Unknown inference tools"):
            await inference.complete(InferenceRequest(prompt="propose", tool_names=("missing",)))
