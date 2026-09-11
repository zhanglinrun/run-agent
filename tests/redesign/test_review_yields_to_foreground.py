"""RED: the review yields to the foreground without losing the request (T-023).

A background review must not compete with the foreground, and its cost must be
attributed to the run that caused it rather than disappearing into an anonymous
total.

Where the guard goes is the whole question, and the first attempt got it wrong. Putting
it on the trigger looked reasonable and broke the pipeline: a completion event arrives
while its own session is still running, so every review would be deferred forever. The
gate belongs at the point a review would start - the worker's consume path - and
deferring there leaves the request queued rather than consumed.

Usage belongs to the parent run because that is what makes the cost attributable. A
review that spends tokens nobody owns is a cost that cannot be explained, so an
unattributed ledger refuses to report rather than reporting anonymously.
"""

import pytest
from extensions.experience.review import ForegroundGate
from extensions.experience.worker import ReviewLedger, UnattributedUsage


def test_a_busy_foreground_defers_the_review():
    assert ForegroundGate(busy=lambda: True).deferral() == "foreground busy"


def test_an_idle_foreground_does_not_defer():
    assert ForegroundGate(busy=lambda: False).deferral() is None


def test_the_gate_reads_the_foreground_state_live():
    state = {"busy": True}
    gate = ForegroundGate(busy=lambda: state["busy"])

    assert gate.deferral() == "foreground busy"
    state["busy"] = False
    assert gate.deferral() is None, "the gate captured a stale foreground state"


def test_a_review_charges_its_usage_to_the_parent_run():
    ledger = ReviewLedger(parent_run_id="run-1")

    assert ledger.charge(requests=1, input_tokens=120, output_tokens=30) is True
    attribution = ledger.attribution()

    assert attribution["parent_run_id"] == "run-1"
    assert attribution["input_tokens"] == 120
    assert attribution["output_tokens"] == 30
    assert attribution["requests"] == 1


def test_an_unattributed_ledger_refuses_to_report_usage():
    ledger = ReviewLedger()

    ledger.charge(requests=1, input_tokens=10, output_tokens=5)

    with pytest.raises(UnattributedUsage, match="parent"):
        ledger.attribution()
