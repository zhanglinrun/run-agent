"""RED: the cost and latency ledger chapter 7 asks a report to carry (T-018).

Chapter 7's selection dimensions include cost and the two latency stages that are
easy to confuse: prefill decides time-to-first-token, decode decides the rest, and p95
tail latency "reflects the real user experience better than the mean, which a crowd of
fast requests drags down while a few users hit a stall."

It also warns that retries change the answer: "a cheap model with a low success rate
may end up costing more because it has to retry often", so the ledger must count
attempts rather than only successful calls. And a cost per success is meaningless when
nothing succeeded, which must be reported as unknown rather than as zero.
"""

import pytest

from run_agent_evals.ledger import CallRecord, EmptyLedger, summarise


def call(
    cost: float, latency: float, *, attempt: int = 1, ttft: float | None = None, ok: bool = True
) -> CallRecord:
    return CallRecord(
        input_tokens=100,
        output_tokens=50,
        cost=cost,
        latency_seconds=latency,
        attempt=attempt,
        time_to_first_token=ttft,
        succeeded=ok,
    )


def test_retries_are_counted_not_hidden():
    summary = summarise(
        (
            call(0.02, 1.0, ok=False),
            call(0.02, 1.2, attempt=2),
            call(0.02, 0.9),
        )
    )

    assert summary.calls == 3
    assert summary.retries == 1
    assert summary.total_cost == pytest.approx(0.06)


def test_the_mean_cost_is_per_call_and_the_cost_per_success_is_per_success():
    summary = summarise((call(0.03, 1.0, ok=False), call(0.01, 1.0), call(0.01, 1.0)))

    assert summary.mean_cost == pytest.approx(0.05 / 3)
    assert summary.cost_per_success == pytest.approx(0.05 / 2)


def test_a_cost_per_success_is_unknown_when_nothing_succeeded():
    summary = summarise((call(0.04, 1.0, ok=False),))

    assert summary.successes == 0
    assert summary.cost_per_success is None
    assert summary.to_json()["cost_per_success"] is None


def test_the_p95_tail_is_reported_alongside_the_mean():
    # Nineteen fast calls and one stall: the mean hides it, the tail does not.
    records = tuple(call(0.01, 0.5) for _ in range(19)) + (call(0.01, 8.0),)

    summary = summarise(records)

    assert summary.mean_latency < 1.0
    assert summary.p95_latency == pytest.approx(8.0)


def test_time_to_first_token_is_reported_when_it_was_measured():
    summary = summarise((call(0.01, 1.0, ttft=0.3), call(0.01, 1.0, ttft=0.5)))

    assert summary.mean_time_to_first_token == pytest.approx(0.4)
    assert summarise((call(0.01, 1.0),)).mean_time_to_first_token is None


def test_an_empty_ledger_is_refused_rather_than_reported_as_zero():
    with pytest.raises(EmptyLedger, match="record"):
        summarise(())


def test_the_summary_serialises_for_the_report():
    payload = summarise((call(0.02, 1.0, ttft=0.2),)).to_json()

    assert payload["calls"] == 1
    assert payload["successes"] == 1
    assert set(payload) >= {
        "calls",
        "retries",
        "successes",
        "total_cost",
        "mean_cost",
        "cost_per_success",
        "mean_latency",
        "p95_latency",
        "mean_time_to_first_token",
    }
