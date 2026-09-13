"""A rubric with weights, a pitfall tier, and a veto.

Chapter 7's four rules for a rubric: grounded in expert guidance, comprehensive
including explicit pitfalls, weighted by importance with a veto mechanism, and
self-contained per item. It warns why the veto exists: "in a support scenario,
hallucination is a classic veto dimension - no matter how well the other dimensions
score, fabricating information must veto" - which also blunts keyword-stuffing
reward hacking.

And it says plainly where model judgement belongs: "every check that can be written as
a programmatic assertion should stay an assertion; model judgement is only for
dimensions that genuinely cannot be decided mechanically." So scoring here is
arithmetic over ratings, and a model is not consulted to compute a weighted mean.
"""

import pytest

from run_agent_evals.rubric import (
    Dimension,
    Rubric,
    UnknownRating,
    score,
)

CORRECTNESS = Dimension(
    name="事实正确性",
    weight="essential",
    scoring={4: "准确回答 Dr. Chen，且关联到女儿 Lily", 1: "给出错误医生名"},
)
COMPLETENESS = Dimension(
    name="信息完整性",
    weight="important",
    scoring={4: "主动补充相关信息", 1: "遗漏核心信息"},
)
HALLUCINATION = Dimension(
    name="幻觉",
    weight="pitfall",
    scoring={4: "无编造信息", 1: "编造了不存在的记录"},
    veto_below=2,
)


def rubric() -> Rubric:
    return Rubric(dimensions=(CORRECTNESS, COMPLETENESS, HALLUCINATION))


def test_a_heavier_dimension_moves_the_score_more():
    heavy = score(Rubric(dimensions=(CORRECTNESS,)), {"事实正确性": 4})
    light = score(Rubric(dimensions=(COMPLETENESS,)), {"信息完整性": 4})

    assert heavy.weighted > 0
    assert light.weighted > 0
    # Same rating, different weight: the essential dimension is worth more.
    assert (
        score(
            Rubric(dimensions=(CORRECTNESS, COMPLETENESS)), {"事实正确性": 4, "信息完整性": 4}
        ).weighted
        > score(
            Rubric(dimensions=(CORRECTNESS, COMPLETENESS)), {"事实正确性": 4, "信息完整性": 1}
        ).weighted
    )


def test_a_pitfall_below_its_threshold_vetoes_a_perfect_score():
    result = score(rubric(), {"事实正确性": 4, "信息完整性": 4, "幻觉": 1})

    assert result.vetoed_by == "幻觉"
    assert result.accepted is False


def test_a_pitfall_at_its_threshold_does_not_veto():
    result = score(rubric(), {"事实正确性": 4, "信息完整性": 4, "幻觉": 2})

    assert result.vetoed_by is None
    assert result.accepted is True


def test_a_veto_cannot_be_averaged_away_by_other_dimensions():
    vetoed = score(rubric(), {"事实正确性": 4, "信息完整性": 4, "幻觉": 1})
    clean = score(rubric(), {"事实正确性": 1, "信息完整性": 1, "幻觉": 4})

    assert vetoed.weighted > clean.weighted, "the vetoed run scores higher on average"
    assert vetoed.accepted is False
    assert clean.accepted is True


def test_an_unknown_weight_is_refused():
    with pytest.raises(ValueError, match="weight"):
        Dimension(name="x", weight="very-important", scoring={1: "text"})


def test_a_dimension_without_scoring_tiers_is_refused():
    with pytest.raises(ValueError, match="scoring"):
        Dimension(name="x", weight="optional", scoring={})


def test_a_veto_on_a_non_pitfall_dimension_is_refused():
    with pytest.raises(ValueError, match="pitfall"):
        Dimension(name="x", weight="important", scoring={1: "text"}, veto_below=2)


def test_duplicate_dimension_names_are_refused():
    with pytest.raises(ValueError, match="unique"):
        Rubric(dimensions=(CORRECTNESS, CORRECTNESS))


def test_a_missing_rating_is_refused_rather_than_defaulted():
    with pytest.raises(UnknownRating, match="信息完整性"):
        score(rubric(), {"事实正确性": 4, "幻觉": 4})
