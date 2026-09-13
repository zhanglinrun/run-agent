"""Attribute the FIRST error, not the last one.

Chapter 7 is explicit about the target: "the object of attribution is the first error
in the trajectory that caused the task to deviate; later errors are usually just
knock-on effects, and the last raised error must not simply be treated as the root
cause." It also fixes the categories, one per coding failure mode, and requires the
attribution to carry reviewable evidence - a step number plus the tool call or model
output - because the point is to drive a fix.

The categories come from the chapter's coding list: missing process, tool-call error,
execution exception, completeness and logic error, requirement misunderstanding and
ambiguity, symptom patching and verification fraud, and feedback.
"""

import pytest

from run_agent_evals.attribution import (
    CATEGORIES,
    Attribution,
    Step,
    attribute,
)


def tool(index: int, detail: str, *, failed: bool = False, category: str | None = None) -> Step:
    return Step(index=index, kind="tool_call", detail=detail, failed=failed, category=category)


def test_the_first_fault_is_the_root_cause_not_the_last_raise():
    steps = (
        tool(1, "read('a.py')"),
        tool(2, "bash('pytest')", failed=True, category="执行异常"),
        tool(3, "write('a.py')", failed=True, category="工具调用错误"),
        tool(4, "bash('pytest')", failed=True, category="执行异常"),
    )

    attribution = attribute(steps)

    assert isinstance(attribution, Attribution)
    assert attribution.first_error_index == 2
    assert attribution.category == "执行异常"
    assert attribution.subsequent_errors == (3, 4)


def test_a_clean_trajectory_attributes_nothing():
    attribution = attribute((tool(1, "read('a.py')"), tool(2, "write('a.py')")))

    assert attribution.first_error_index is None
    assert attribution.category is None
    assert attribution.evidence == ()


def test_every_category_is_attributable():
    for category in CATEGORIES:
        steps = (tool(1, "step"), tool(2, "fault", failed=True, category=category))
        assert attribute(steps).category == category, category


def test_an_unknown_category_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="unknown category"):
        attribute((tool(1, "fault", failed=True, category="随便编的"),))


def test_a_fault_without_a_category_is_still_the_root_cause():
    attribution = attribute((tool(1, "ok"), tool(2, "boom", failed=True)))

    assert attribution.first_error_index == 2
    assert attribution.category is None


def test_the_evidence_identifies_the_step_and_what_it_did():
    attribution = attribute((tool(1, "ok"), tool(2, "bash('pytest')", failed=True)))

    assert attribution.evidence == ("step 2 tool_call: bash('pytest')",)


def test_the_chapter_categories_are_all_present():
    assert CATEGORIES == (
        "流程缺失",
        "工具调用错误",
        "执行异常",
        "完成度与逻辑错误",
        "需求理解与歧义",
        "症状修复与验证造假",
        "信息反馈",
    )
