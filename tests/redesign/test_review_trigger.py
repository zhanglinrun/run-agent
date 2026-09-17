"""Which finished runs deserve a review, and how often.

A durable completion is not by itself a reason to review. The policy has to pick
out the runs worth learning from, never review the same run twice, respect a
cooldown, and never review an auxiliary task - otherwise a review triggers a
review and the loop never ends.
"""

import pytest

from run_agent_extensions.experience.review import (
    ReviewPolicy,
    ReviewRequest,
    ReviewTrigger,
)


class Clock:
    """A controllable clock so cooldown is asserted without sleeping."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def request(**overrides) -> ReviewRequest:
    fields = {
        "source_run_id": "run-1",
        "session_id": "session-1",
        "status": "succeeded",
        "assistant_turns": 4,
        "corrections": 0,
        "failures": 0,
        "origin_kind": "user",
    }
    return ReviewRequest(**{**fields, **overrides})


def test_a_substantial_successful_run_is_reviewed_with_a_stable_key() -> None:
    trigger = ReviewTrigger()
    decision = trigger.consider(request(skills_due=True))
    assert decision.admitted is True
    assert decision.key == "run-1:1"


def test_the_same_run_is_never_reviewed_twice() -> None:
    trigger = ReviewTrigger()
    first = trigger.consider(request(skills_due=True))
    second = trigger.consider(request(skills_due=True))
    assert first.admitted is True
    assert second.admitted is False
    assert second.reason == "already reviewed"
    assert second.key == first.key


def test_chitchat_and_trivial_completions_are_not_reviewed() -> None:
    trigger = ReviewTrigger()
    decision = trigger.consider(request(assistant_turns=1))
    assert decision.admitted is False
    assert decision.reason == "not worth reviewing"


def test_a_correction_or_a_repeat_failure_is_worth_reviewing() -> None:
    clock = Clock()
    trigger = ReviewTrigger(clock=clock, policy=ReviewPolicy(review_on_signals=True))
    corrected = trigger.consider(request(source_run_id="run-2", corrections=1))
    clock.advance(ReviewPolicy().cooldown_seconds + 1)
    failed = trigger.consider(request(source_run_id="run-3", failures=2))
    assert corrected.admitted is True
    assert failed.admitted is True


def test_signals_do_not_admit_a_review_by_default() -> None:
    trigger = ReviewTrigger()
    decision = trigger.consider(request(corrections=1, failures=1))
    assert decision.admitted is False
    assert decision.reason == "not worth reviewing"


def test_an_auxiliary_task_never_triggers_a_review() -> None:
    trigger = ReviewTrigger()
    for origin in ("review", "evaluation", "naming"):
        decision = trigger.consider(request(source_run_id=f"aux-{origin}", origin_kind=origin))
        assert decision.admitted is False, origin
        assert decision.reason == "auxiliary task"


def test_a_second_review_waits_for_the_cooldown() -> None:
    clock = Clock()
    trigger = ReviewTrigger(clock=clock, policy=ReviewPolicy(review_on_signals=True))
    assert trigger.consider(request(source_run_id="run-4", corrections=1)).admitted is True
    cooling = trigger.consider(request(source_run_id="run-5", corrections=1))
    assert cooling.admitted is False
    assert cooling.reason == "cooling down"
    clock.advance(ReviewPolicy().cooldown_seconds + 1)
    assert trigger.consider(request(source_run_id="run-6", corrections=1)).admitted is True


def test_a_policy_change_lets_an_already_reviewed_run_be_reviewed_again() -> None:
    trigger = ReviewTrigger(policy=ReviewPolicy(policy_version="1"))
    assert trigger.consider(request(source_run_id="run-7", skills_due=True)).admitted is True
    assert trigger.consider(request(source_run_id="run-7", skills_due=True)).admitted is False
    upgraded = ReviewTrigger(policy=ReviewPolicy(policy_version="2"))
    decision = upgraded.consider(request(source_run_id="run-7", skills_due=True))
    assert decision.admitted is True
    assert decision.key == "run-7:2"


def test_review_requests_are_frozen() -> None:
    frozen = request()
    with pytest.raises((AttributeError, TypeError)):
        frozen.origin_kind = "review"
