"""The metrics chapter 7 defines, with the book's own numbers as anchors.

Chapter 7 is precise about how success must be defined and reported:

    Pass@k = 1 - (1 - p)^k        Pass^k = p^k
    SE(p)  ~ sqrt(p (1 - p) / n)

and it warns that a difference of a few points on a few dozen cases is noise, so
two configurations on the same tasks must be compared *paired* - per task, who won -
rather than by subtracting two independent rates.

The citations are the point of these tests: p=0.6 at k=5 must give the chapter's
99.0% and 7.8%, and 70 successes in 100 must give the chapter's "+-9 points".
"""

import pytest

from run_agent_evals.statistics import (
    PairedComparison,
    Ratio,
    SeedSummary,
    confidence_interval,
    observed_all_passed,
    observed_at_least_one,
    pass_at_k,
    pass_power_k,
    summarize_seeds,
    trials_for_margin,
)


def test_the_book_worked_example_for_pass_at_k_and_pass_power_k() -> None:
    # Chapter 7: p=0.6, k=5 -> Pass@5 ~ 99.0%, Pass^5 ~ 7.8%.
    assert pass_at_k(0.6, 5) == pytest.approx(0.990, abs=0.001)
    assert pass_power_k(0.6, 5) == pytest.approx(0.078, abs=0.001)


def test_pass_at_one_is_the_rate_itself() -> None:
    assert pass_at_k(0.73, 1) == pytest.approx(0.73)


def test_the_book_worked_example_for_the_confidence_interval() -> None:
    # Chapter 7: 100 cases at 70% -> "95% confidence interval is about +-9 points".
    low, high = confidence_interval(0.7, 100)
    assert (high - 0.7) == pytest.approx(0.09, abs=0.002)
    assert (0.7 - low) == pytest.approx(0.09, abs=0.002)


def test_the_standard_error_shrinks_with_the_square_root_of_the_sample() -> None:
    assert Ratio(70, 100).standard_error == pytest.approx(0.0458, abs=0.0001)
    # Quadrupling the sample halves the interval, as 1/sqrt(n) requires.
    wide = Ratio(70, 100).interval()
    narrow = Ratio(280, 400).interval()
    assert (narrow[1] - narrow[0]) == pytest.approx((wide[1] - wide[0]) / 2, abs=0.002)


def test_a_rate_without_trials_is_refused_rather_than_reported_as_zero() -> None:
    with pytest.raises(ValueError, match="trials"):
        Ratio(0, 0)


def test_paired_comparison_matches_exact_mcnemar_on_discordant_pairs() -> None:
    # b=10, c=2 over 12 discordant pairs: 2 * (C(12,0)+C(12,1)+C(12,2)) / 2**12.
    left = [True] * 10 + [False] * 2 + [True] * 8 + [False] * 5
    right = [False] * 10 + [True] * 2 + [True] * 8 + [False] * 5
    result = PairedComparison.of(left, right)

    assert (result.left_only, result.right_only) == (10, 2)
    assert result.difference == pytest.approx(8 / 25)
    assert result.exact_p_value == pytest.approx(158 / 4096)
    assert result.is_significant(0.05) is True


def test_identical_configurations_show_no_difference() -> None:
    outcomes = [True, False, True, True, False]
    result = PairedComparison.of(outcomes, outcomes)

    assert result.difference == 0.0
    assert result.exact_p_value == 1.0
    assert result.is_significant(0.05) is False


def test_paired_comparison_needs_the_same_tasks_on_both_sides() -> None:
    with pytest.raises(ValueError, match="same tasks"):
        PairedComparison.of([True, False], [True])


def test_observed_metrics_count_at_least_one_and_all_across_attempts() -> None:
    attempts = [[True, False, False], [False, False, False], [True, True, True]]

    assert observed_at_least_one(attempts).rate == pytest.approx(2 / 3)
    assert observed_all_passed(attempts).rate == pytest.approx(1 / 3)


def test_seeds_are_reported_as_a_mean_with_its_range() -> None:
    summary = summarize_seeds([0.60, 0.70, 0.65])

    assert isinstance(summary, SeedSummary)
    assert summary.mean == pytest.approx(0.65)
    assert (summary.low, summary.high) == (0.60, 0.70)


def test_trials_needed_grows_as_the_expected_difference_shrinks() -> None:
    coarse = trials_for_margin(0.10, 0.7)
    fine = trials_for_margin(0.02, 0.7)

    assert fine > coarse
    assert fine == pytest.approx(2017, rel=0.02)
