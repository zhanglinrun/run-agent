"""RED: the two propositions chapter 7 requires before "fixed" means anything (T-052).

Chapter 7 is explicit that one group of tests is not enough:

    FAIL_TO_PASS: failed before the fix, passes after  -> the problem is really solved
    PASS_TO_PASS: passed before and after              -> nothing else was broken
    "checking only the first, an agent can sneak through by deleting or editing
     the assertions that get in its way; checking only the second is not checking
     at all."

So a verdict must rest on both, and a test that disappears from the candidate must
never be silently treated as passing - that is exactly the deletion this guards
against. Chapter 7 also requires confirming the tests' own stability and excluding
the ones that pass and fail at random, because a verdict resting on a flaky test is
not a verdict.

Classification is pure and runs against a fake suite capability, so these tests are
deterministic and never spawn a subprocess.
"""

import pytest

from run_agent_evals.verifier import DualProposition, SuiteResult, classify


def test_a_test_that_failed_before_and_passes_after_is_the_proof_of_fix() -> None:
    pristine = SuiteResult.of_single({"target": False, "kept": True})
    candidate = SuiteResult.of_single({"target": True, "kept": True})

    verdict = classify(pristine, candidate)

    assert isinstance(verdict, DualProposition)
    assert verdict.fail_to_pass == frozenset({"target"})
    assert verdict.pass_to_pass == frozenset({"kept"})
    assert verdict.is_fix_proven is True
    assert verdict.is_regression_free is True
    assert verdict.succeeded is True


def test_breaking_a_previously_passing_test_defeats_the_verdict() -> None:
    # This is the case "checking only FAIL_TO_PASS" would wave through.
    pristine = SuiteResult.of_single({"target": False, "kept": True})
    candidate = SuiteResult.of_single({"target": True, "kept": False})

    verdict = classify(pristine, candidate)

    assert verdict.newly_failing == frozenset({"kept"})
    assert verdict.is_fix_proven is True
    assert verdict.is_regression_free is False
    assert verdict.succeeded is False


def test_a_deleted_test_is_not_a_passing_test() -> None:
    # The cheapest way to make an inconvenient assertion stop failing.
    pristine = SuiteResult.of_single({"target": False, "kept": True})
    candidate = SuiteResult.of_single({"target": True})

    verdict = classify(pristine, candidate)

    assert "kept" in verdict.newly_failing
    assert verdict.is_regression_free is False
    assert verdict.succeeded is False


def test_a_test_only_the_candidate_added_cannot_claim_the_fix() -> None:
    pristine = SuiteResult.of_single({"target": False})
    candidate = SuiteResult.of_single({"target": False, "self_serving": True})

    verdict = classify(pristine, candidate)

    assert verdict.fail_to_pass == frozenset()
    assert verdict.is_fix_proven is False
    assert verdict.succeeded is False


def test_a_target_that_is_still_failing_is_not_a_fix() -> None:
    pristine = SuiteResult.of_single({"target": False, "kept": True})
    candidate = SuiteResult.of_single({"target": False, "kept": True})

    verdict = classify(pristine, candidate)

    assert verdict.still_failing == frozenset({"target"})
    assert verdict.succeeded is False


def test_a_test_that_passes_and_fails_at_random_is_excluded_not_counted() -> None:
    # Chapter 7: confirm the tests' own stability and exclude the unstable ones.
    pristine = SuiteResult.of(runs=({"target": False, "kept": True},) * 2)
    candidate = SuiteResult.of(
        runs=({"target": True, "kept": True}, {"target": False, "kept": True})
    )

    verdict = classify(pristine, candidate)

    assert verdict.flaky == frozenset({"target"})
    assert verdict.fail_to_pass == frozenset()
    assert verdict.succeeded is False


def test_stability_is_decided_by_repeated_runs_of_one_unchanged_state() -> None:
    stable = SuiteResult.of(runs=({"a": True}, {"a": True}))
    unstable = SuiteResult.of(runs=({"a": True}, {"a": False}))

    assert stable.flaky == frozenset()
    assert unstable.flaky == frozenset({"a"})
    assert stable.unanimous == {"a": True}


def test_an_empty_suite_cannot_prove_anything() -> None:
    verdict = classify(SuiteResult.of_single({}), SuiteResult.of_single({}))

    assert verdict.succeeded is False
    assert verdict.is_fix_proven is False


def test_a_suite_result_needs_at_least_one_run() -> None:
    with pytest.raises(ValueError, match="run"):
        SuiteResult.of(runs=())
