"""RED: a timed-out eventually() must say what it was waiting for and how long.

The gate failed once on test_mixed_load with a bare TimeoutError and nothing else, and
that report is why the failure is still uncharacterised. Twelve measured rounds drain
in 0.92-1.08 seconds against a five-second budget, and the same measurement under load
gives the same numbers, so "five seconds is too tight" is not supported - which means
the next occurrence has to be informative rather than assumed.

This pins that: a timed-out wait reports the elapsed time and the check it was polling,
so the next failure says whether it was slow or stuck.
"""

import asyncio

import pytest
from tests.redesign.test_gateway_runtime import eventually


async def test_a_satisfied_condition_returns_its_value():
    assert await eventually(lambda: _truthy(True), timeout=1) is True


async def test_a_timeout_reports_the_elapsed_time_and_the_check():
    async def never():
        return False

    with pytest.raises(AssertionError) as raised:
        await eventually(never, timeout=0.05)
    message = str(raised.value)

    assert "0.05s" in message, message
    assert "budget" in message, message
    assert "never" in message, "the report must name what was being waited for"


async def test_a_condition_that_becomes_true_late_still_reports_success():
    state = {"ready": False}

    async def settles():
        return state["ready"]

    async def flip():
        await asyncio.sleep(0.05)
        state["ready"] = True

    asyncio.create_task(flip())
    assert await eventually(settles, timeout=2) is True


async def _truthy(value):
    return value
