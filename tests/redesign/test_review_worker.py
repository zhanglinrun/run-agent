"""RED: the review worker's budget and its capability limits (P5-2).

A review fork runs a model like anything else, so it has to be bounded the way
Hermes bounds its background review: an explicit input-token budget that a
non-positive value switches off, a hard request count that retries also consume,
and one review per principal at a time so a principal cannot multiply its own
spending.

It also has to be unable to do harm. The fork may read evidence and propose a
candidate; it may never publish, edit a published asset, touch permission
configuration, or write the main workspace.
"""

import pytest
from extensions.experience.worker import (
    ReviewBudget,
    ReviewCapabilities,
    ReviewCapabilityError,
    ReviewLedger,
    ReviewWorker,
)


def test_the_budget_defaults_to_four_requests_and_counts_retries() -> None:
    ledger = ReviewLedger()
    assert ReviewBudget().max_model_requests == 4
    for _ in range(4):
        assert ledger.charge(requests=1, input_tokens=100, output_tokens=50) is True
    # A retry is a request like any other, so the fifth call is refused.
    assert ledger.charge(requests=1, input_tokens=10, output_tokens=5) is False
    assert ledger.requests == 5
    assert ledger.exhausted is True


def test_the_input_token_budget_is_explicit_and_can_be_switched_off() -> None:
    capped = ReviewBudget(max_input_tokens=1_000)
    ledger = ReviewLedger(capped)
    assert ledger.charge(requests=1, input_tokens=900, output_tokens=1) is True
    assert ledger.charge(requests=1, input_tokens=200, output_tokens=1) is False

    unlimited = ReviewLedger(ReviewBudget(max_input_tokens=0))
    assert unlimited.budget.unlimited_input is True
    assert unlimited.charge(requests=1, input_tokens=10_000_000, output_tokens=1) is True


def test_the_ledger_reports_remaining_capacity() -> None:
    ledger = ReviewLedger(ReviewBudget(max_model_requests=2, max_input_tokens=500))
    ledger.charge(requests=1, input_tokens=200, output_tokens=10)
    assert ledger.remaining_requests == 1
    assert ledger.remaining_input_tokens == 300


def test_the_review_may_read_evidence_and_propose_but_not_publish() -> None:
    capabilities = ReviewCapabilities()
    for allowed in ("read_evidence", "create_candidate", "inspect"):
        assert capabilities.allows(allowed) is True, allowed
    for refused in ("publish", "edit_published", "edit_permissions", "write_main_workspace"):
        assert capabilities.allows(refused) is False, refused
        with pytest.raises(ReviewCapabilityError, match=refused):
            capabilities.require(refused)


def test_an_unknown_capability_is_refused_rather_than_allowed() -> None:
    capabilities = ReviewCapabilities()
    assert capabilities.allows("delete_everything") is False
    with pytest.raises(ReviewCapabilityError):
        capabilities.require("delete_everything")


def test_only_one_review_runs_per_principal() -> None:
    worker = ReviewWorker()
    claim = worker.claim("alice")
    assert claim is not None
    assert worker.claim("alice") is None, "a second review for the same principal"
    assert worker.claim("bob") is not None, "another principal is independent"
    worker.release(claim)
    assert worker.claim("alice") is not None


def test_a_released_claim_cannot_be_released_twice() -> None:
    worker = ReviewWorker()
    claim = worker.claim("alice")
    assert claim is not None
    worker.release(claim)
    with pytest.raises(KeyError):
        worker.release(claim)
