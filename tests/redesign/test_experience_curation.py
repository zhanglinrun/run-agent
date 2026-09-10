"""RED: character budgets, time transitions and pinned protection (T-023).

Aligned with the Hermes reference recorded in docs/implementation/hermes-alignment.md.
Three things it does that this project did not:

  character budgets   Hermes limits USER and MEMORY by characters (1375 and 2200), not
                      by tokens, because the limit is about what fits in a prompt, and
                      it returns the usage when a write would exceed it rather than
                      silently truncating
  time transitions    an asset nobody uses goes stale at 30 days and archive-eligible
                      at 90, so the store does not grow forever
  pinned protection   a user-pinned asset is exempt from both, because automatic
                      maintenance must not delete what the user kept

A dry run must be decidable, not decorative: it reports the same decision with
``applied`` false, so calling it gives the answer without changing anything.
"""

from datetime import UTC, datetime, timedelta

import pytest
from extensions.experience.curation import (
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    BudgetExceeded,
    CharacterBudget,
    CurationAction,
    curate,
)

NOW = datetime(2026, 1, 31, tzinfo=UTC)


def test_the_limits_are_characters_and_match_the_reference():
    assert (USER_CHAR_LIMIT, MEMORY_CHAR_LIMIT) == (1375, 2200)


def test_a_write_within_the_limit_reports_its_usage():
    budget = CharacterBudget(limit=20)

    usage = budget.reserve("0123456789", existing=("0123456789",))

    assert (usage.used, usage.remaining, usage.limit) == (20, 0, 20)


def test_a_write_over_the_limit_is_refused_with_the_usage_not_truncated():
    budget = CharacterBudget(limit=10)

    with pytest.raises(BudgetExceeded) as raised:
        budget.reserve("hello", existing=("0123456789",))

    assert raised.value.usage.used == 15
    assert raised.value.usage.remaining == 0
    assert "15" in str(raised.value)


def test_characters_not_tokens_are_counted():
    # Eight characters, two tokens at most: a token budget would let this through.
    budget = CharacterBudget(limit=5)

    with pytest.raises(BudgetExceeded):
        budget.reserve("abcdefgh")


def test_an_asset_untouched_for_thirty_days_goes_stale():
    decision = curate(last_used=NOW - timedelta(days=31), now=NOW, pinned=False)

    assert decision.action is CurationAction.STALE
    assert decision.applied is True


def test_an_asset_untouched_for_ninety_days_becomes_archive_eligible():
    decision = curate(last_used=NOW - timedelta(days=91), now=NOW, pinned=False)

    assert decision.action is CurationAction.ARCHIVE
    assert "90" in decision.reason


def test_a_recently_used_asset_is_left_alone():
    decision = curate(last_used=NOW - timedelta(days=3), now=NOW, pinned=False)

    assert decision.action is CurationAction.KEEP


def test_a_pinned_asset_is_exempt_from_both_transitions():
    old = NOW - timedelta(days=365)

    assert curate(last_used=old, now=NOW, pinned=True).action is CurationAction.KEEP


def test_a_dry_run_reaches_the_same_decision_without_applying_it():
    arguments = {"last_used": NOW - timedelta(days=100), "now": NOW, "pinned": False}

    real = curate(**arguments)
    dry = curate(**arguments, dry_run=True)

    assert dry.action is real.action is CurationAction.ARCHIVE
    assert dry.applied is False
    assert real.applied is True
