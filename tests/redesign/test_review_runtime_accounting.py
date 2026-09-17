"""Review budgets, unknown usage, and local cancellation must remain observable."""

import asyncio
from types import SimpleNamespace

import pytest

from run_agent_coding.host.inference import InferenceResult
from run_agent_core.messages import TextContent, ToolCall
from run_agent_core.tools import AgentToolResult
from run_agent_extensions.experience.commands_learning import register_learning_commands
from run_agent_extensions.experience.review import ReviewCoordinator
from run_agent_extensions.experience.review_agent import ReviewRun, run_tool_loop
from run_agent_extensions.experience.review_models import ReviewPolicy
from run_agent_extensions.experience.worker import ReviewBudget, ReviewLedger


async def tool(*_):
    return AgentToolResult(content=[TextContent(text="saved")])


async def loop(inference, ledger, *, run=None, execute=tool, **kwargs):
    return await run_tool_loop(
        inference=inference,
        ledger=ledger,
        run=run,
        execute=execute,
        system="system",
        conversation="conversation",
        instruction="review",
        purpose="test",
        **kwargs,
    )


def test_configured_host_and_execution_budgets_agree():
    policy = ReviewPolicy(max_iterations=7, max_input_tokens=12_345)
    assert policy.budget == policy.execution_budget
    assert policy.budget.max_model_requests == 7
    assert policy.budget.max_input_tokens == 12_345


async def test_timeout_counts_attempt_and_keeps_usage_unknown():
    async def complete(_):
        raise TimeoutError("provider timed out")

    ledger = ReviewLedger(parent_run_id="source")
    result = await loop(SimpleNamespace(complete=complete), ledger)
    usage = ledger.attribution()
    assert result.error == "TimeoutError: provider timed out"
    assert result.iterations == usage["requests"] == 1
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["unreported_requests"] == 1
    assert usage["estimated_input_tokens"] > 0


async def test_request_cap_is_checked_before_another_inference():
    async def complete(_):
        return InferenceResult('{"tool":"memory","args":{}}', "test", "snapshot")

    ledger = ReviewLedger(ReviewBudget(max_model_requests=2), parent_run_id="source")
    result = await loop(SimpleNamespace(complete=complete), ledger, max_iterations=16)
    assert result.stop_reason == "iteration budget exhausted"
    assert ledger.requests == len(result.calls) == 2
    assert ledger.attribution()["input_tokens"] is None


async def test_preflight_includes_system_and_stops_before_spending():
    async def complete(_):
        pytest.fail("over-budget request must not start")

    ledger = ReviewLedger(ReviewBudget(max_input_tokens=1), parent_run_id="source")
    result = await loop(SimpleNamespace(complete=complete), ledger)
    assert result.stop_reason == "input budget exhausted"
    assert ledger.requests == 0


async def test_native_calls_use_selected_tools_and_return_results_to_next_iteration():
    requests = []
    executed = []

    async def complete(request):
        requests.append(request)
        assert request.tool_names == ("memory", "skill_manage")
        if len(requests) == 1:
            return InferenceResult(
                "",
                "test",
                "snapshot",
                input_tokens=10,
                output_tokens=10,
                tool_calls=(
                    ToolCall(id="memory-one", name="memory", arguments={"action": "add"}),
                    ToolCall(id="skill-one", name="skill_manage", arguments={"action": "create"}),
                ),
            )
        assert "TOOL RESULT (memory): saved" in request.prompt
        assert "TOOL RESULT (skill_manage): saved" in request.prompt
        return InferenceResult("Saved.", "test", "snapshot", input_tokens=10, output_tokens=10)

    async def execute(name, arguments):
        executed.append(name)
        return await tool()

    ledger = ReviewLedger(parent_run_id="source")
    result = await loop(
        SimpleNamespace(complete=complete),
        ledger,
        execute=execute,
        tool_names=("memory", "skill_manage"),
    )
    assert executed == ["memory", "skill_manage"]
    assert ledger.requests == 2
    assert result.final_text == "Saved."


@pytest.mark.parametrize(
    ("answer", "cap", "expected"),
    [
        ('{"tool":"memory","args":{}}{"tool":"skill_manage","args":{}}Saved.', 4, 2),
        ('{"tool":"memory","args":{}}{broken', 4, 0),
        ('Example: {"tool":"memory","args":{}}', 4, 0),
        ('{"tool":"memory","args":{}}{"tool":"skill_manage","args":{}}Saved.', 1, 0),
    ],
)
async def test_consecutive_tool_calls_are_validated_and_bounded_before_writing(
    answer, cap, expected
):
    executed = []

    async def complete(_):
        return InferenceResult(answer, "test", "snapshot", input_tokens=10, output_tokens=10)

    async def execute(name, arguments):
        executed.append(name)
        return await tool()

    result = await loop(
        SimpleNamespace(complete=complete),
        ReviewLedger(parent_run_id="source"),
        execute=execute,
        max_iterations=cap,
    )
    assert len(executed) == len(result.calls) == expected
    if expected:
        assert executed == ["memory", "skill_manage"]
        assert result.final_text == "Saved."
    if cap == 1:
        assert result.stop_reason == "tool call budget exhausted"


async def test_cancellation_between_consecutive_calls_prevents_remaining_writes():
    run = ReviewRun()

    async def complete(_):
        return InferenceResult(
            '{"tool":"memory","args":{}}{"tool":"skill_manage","args":{}}Saved.',
            "test",
            "snapshot",
            input_tokens=10,
            output_tokens=10,
        )

    async def execute(*_):
        run.cancel_requested.set()
        return await tool()

    result = await loop(
        SimpleNamespace(complete=complete),
        ReviewLedger(parent_run_id="source"),
        run=run,
        execute=execute,
    )
    assert len(result.calls) == 1
    assert result.stop_reason == "superseded by a new live turn"
    assert result.final_text == ""


@pytest.mark.parametrize("outer_cancel", [False, True])
async def test_cancellation_cleans_up_inference_and_prevents_tool_write(outer_cancel):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    run = ReviewRun()
    ledger = ReviewLedger(parent_run_id="source")

    async def complete(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return InferenceResult('{"tool":"memory","args":{}}', "test", "snapshot")

    async def forbidden_write(*_):
        pytest.fail("cancelled inference must never reach a write")

    task = asyncio.create_task(
        loop(
            SimpleNamespace(complete=complete),
            ledger,
            run=run,
            execute=forbidden_write,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    if outer_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        assert await run.cancel_and_wait(1)
        assert (await task).stop_reason == "superseded by a new live turn"
    assert cleaned.is_set()
    assert run.cancel_requested.is_set()
    assert not run.active
    assert ledger.requests == 1


async def test_late_response_after_cancellation_cannot_write():
    started = asyncio.Event()
    run = ReviewRun()

    async def complete(_):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return InferenceResult('{"tool":"memory","args":{}}', "test", "snapshot")

    async def forbidden_write(*_):
        pytest.fail("a cancellation-suppressing provider cannot authorize a late write")

    task = asyncio.create_task(
        loop(
            SimpleNamespace(complete=complete),
            ReviewLedger(),
            run=run,
            execute=forbidden_write,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    assert await run.cancel_and_wait(1)
    assert (await task).calls == ()


def test_failure_notification_contains_diagnostic_error():
    notifications = []
    api = SimpleNamespace(context=SimpleNamespace(is_running=False), notify=notifications.append)
    coordinator = ReviewCoordinator(api, lambda: None)
    coordinator._announce({"consumed": "source", "status": "failed", "error": "TimeoutError"})
    assert notifications == ["review failed: TimeoutError"]


async def test_review_status_exposes_failure_and_error():
    commands = {}
    api = SimpleNamespace(
        register_command=lambda name, handler, **_: commands.update({name: handler})
    )
    coordinator = SimpleNamespace(
        last_outcome={
            "consumed": "source",
            "status": "failed",
            "error": "TimeoutError",
            "stop_reason": "error",
        }
    )
    config = SimpleNamespace(
        review_enabled=True,
        review_every_turns=10,
        review_cooldown_seconds=900,
        review_notify="on",
        review_on_signals=False,
    )
    register_learning_commands(
        api,
        config=lambda: config,
        stores=lambda: None,
        curator=lambda: None,
        coordinator=coordinator,
        ask=None,
    )
    text = await commands["review"]("status", None)
    assert "source: failed" in text
    assert "error: TimeoutError" in text


async def test_live_turn_wait_is_finite_when_provider_delays_cancellation():
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()
    run = ReviewRun()

    async def complete(_):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            return InferenceResult('{"tool":"memory","args":{}}', "test", "snapshot")

    async def forbidden_write(*_):
        pytest.fail("late completion must not write")

    task = asyncio.create_task(
        loop(
            SimpleNamespace(complete=complete),
            ReviewLedger(),
            run=run,
            execute=forbidden_write,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    assert not await run.cancel_and_wait(0)
    await asyncio.wait_for(cancelling.wait(), 1)
    assert not task.done()
    release.set()
    assert (await asyncio.wait_for(task, 1)).calls == ()
