"""Minimal runtime-simulation support for the runtime and learning suites.

These tests define the contract for three pieces of reusable support the later
Runtime/learning suites need: deterministic fault injection, controllable tools,
and event-conditioned multi-turn input. They exercise the public contract only;
no private helpers are touched.
"""

import asyncio
from time import monotonic

import pytest

from run_agent_evals.simulation import ControlledTool, FaultJournal, InjectedFault, TurnScript


def test_fault_journal_records_order_and_raises_only_where_configured():
    journal = FaultJournal()
    journal.fail_at("commit", error="injected at commit")

    journal.hit("admit")
    assert journal.points() == ("admit",)

    with pytest.raises(InjectedFault) as failure:
        journal.hit("commit")
    assert str(failure.value) == "injected at commit"

    journal.hit("deliver")
    assert journal.points() == ("admit", "commit", "deliver")
    assert journal.failed_at() == ("commit",)


def test_fault_journal_without_configuration_never_raises():
    journal = FaultJournal()
    for point in ("admit", "run", "commit", "deliver"):
        journal.hit(point)
    assert journal.points() == ("admit", "run", "commit", "deliver")
    assert journal.failed_at() == ()


async def test_controlled_tool_counts_invocations_and_records_its_delay():
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    tool = ControlledTool(name="bash", delay=0.05, sleep=record)
    assert tool.calls == 0

    first = await tool.invoke({"command": "true"})
    assert tool.calls == 1
    assert slept == [0.05]
    assert first.arguments == {"command": "true"}

    tool.fail_next("disk full")
    with pytest.raises(InjectedFault) as failure:
        await tool.invoke({"command": "false"})
    assert str(failure.value) == "disk full"
    assert tool.calls == 2

    await tool.invoke({"command": "true"})
    assert tool.calls == 3


async def test_controlled_tool_delay_actually_suspends_when_not_injected():
    """The real delay must really wait.

    The bound is deliberately loose: real timers fire early on this platform (a
    requested 0.05s was measured returning at 0.047s), so asserting >= the exact
    delay is flaky. A bound this far below the request still fails whenever the
    delay is ignored entirely.
    """
    tool = ControlledTool(name="bash", delay=0.05)
    started = monotonic()
    await tool.invoke({"command": "true"})
    assert monotonic() - started >= 0.03


async def test_controlled_tool_runs_all_callers_concurrently_when_delay_is_zero():
    tool = ControlledTool(name="bash")
    results = await asyncio.gather(*(tool.invoke({"command": str(i)}) for i in range(5)))
    assert tool.calls == 5
    assert [item.arguments["command"] for item in results] == ["0", "1", "2", "3", "4"]


def test_turn_script_fires_only_on_its_triggering_event():
    script = TurnScript()
    script.when("tool_started", "stop")
    script.when("run_running", "/status")
    assert script.pending() == 2

    assert script.on_event("tool_finished") is None
    assert script.pending() == 2

    assert script.on_event("tool_started") == "stop"
    assert script.on_event("run_running") == "/status"
    assert script.pending() == 0
    assert script.unfired() == ()


def test_turn_script_reports_what_never_fired_and_does_not_repeat():
    script = TurnScript()
    script.when("checkpoint_committed", "continue")
    script.when("never_happens", "unused")

    assert script.on_event("checkpoint_committed") == "continue"
    assert script.on_event("checkpoint_committed") is None
    assert script.unfired() == ("never_happens",)


def test_turn_script_rejects_a_duplicate_trigger_for_the_same_event():
    script = TurnScript()
    script.when("tool_started", "first")
    with pytest.raises(ValueError, match="tool_started"):
        script.when("tool_started", "second")
