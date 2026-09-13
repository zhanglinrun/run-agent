"""Turn an attributed failure into a regression task, and keep the holdout clean.

Chapter 7 closes the loop this way: once the first error and its category are known,
"you can construct the evaluation dataset, including both end-to-end and
trajectory-prefix regression tasks". It then gives the construction per category - a
symptom-patching or verification-fraud failure, for instance, should add two hard
constraints: "do not modify the test assertions" and "a completion claim must carry
the real command output that was run".

It also names three origins for evaluation sets - public benchmarks for coarse
screening, a self-built set for product decisions, and production trajectory reflow,
which is the most expensive and most accurate because it comes from what users
actually hit.

V05 is the holdout: after learning, the retained set must still be run, and material
that was learned from must not be inside it, or the number measures memorisation.
V06 is reproduction: the report must be rebuildable from stored evidence without
calling a model again, otherwise "we can recompute it" is a claim nobody can check.
"""

import pytest

from run_agent_evals.attribution import Attribution
from run_agent_evals.reflow import (
    CONSTRUCTION,
    ORIGINS,
    Holdout,
    LearnedMaterialInHoldout,
    RegressionTask,
    reflow,
)
from run_agent_evals.suite import SuiteReport, rederive


def attribution(category: str) -> Attribution:
    return Attribution(first_error_index=2, category=category, evidence=("step 2 tool_call: x",))


def test_every_chapter_category_has_a_construction():
    assert set(CONSTRUCTION) == {
        "流程缺失",
        "工具调用错误",
        "执行异常",
        "完成度与逻辑错误",
        "需求理解与歧义",
        "症状修复与验证造假",
        "信息反馈",
    }


def test_a_symptom_patching_failure_becomes_a_task_with_the_two_hard_constraints():
    task = reflow(
        attribution("症状修复与验证造假"),
        task_id="regression-1",
        origin="production_trajectory",
    )

    assert isinstance(task, RegressionTask)
    assert task.kind == "trajectory_prefix"
    assert task.origin == "production_trajectory"
    assert any("assert" in constraint for constraint in task.constraints)
    assert any("command output" in constraint for constraint in task.constraints)


def test_a_missing_process_failure_becomes_an_end_to_end_task():
    task = reflow(attribution("流程缺失"), task_id="regression-2", origin="self_built")

    assert task.kind == "end_to_end"
    assert any("acceptance" in constraint for constraint in task.constraints)


def test_an_ambiguous_requirement_puts_asking_first_among_the_acceptable_actions():
    task = reflow(attribution("需求理解与歧义"), task_id="r3", origin="self_built")

    assert task.kind == "trajectory_prefix"
    assert "ask first" in " ".join(task.constraints).lower()


def test_an_unknown_origin_is_refused():
    with pytest.raises(ValueError, match="origin"):
        reflow(attribution("流程缺失"), task_id="r4", origin="made-up")


def test_a_failure_without_a_category_cannot_be_reflowed():
    with pytest.raises(ValueError, match="categor"):
        reflow(Attribution(), task_id="r5", origin="self_built")


def test_the_three_origins_are_those_chapter_seven_names():
    assert ORIGINS == ("public_benchmark", "self_built", "production_trajectory")


def test_learned_material_may_not_sit_inside_the_holdout():
    holdout = Holdout(task_ids=frozenset({"kept-1", "kept-2"}))

    holdout.verify_disjoint(("new-1", "new-2"))
    with pytest.raises(LearnedMaterialInHoldout, match="kept-1"):
        holdout.verify_disjoint(("new-1", "kept-1"))


def test_the_report_is_rebuildable_from_stored_evidence_without_calling_a_model():
    stored = {
        "task-a": {"g::t1": False, "g::t2": True},
        "task-b": {"g::t1": False, "g::t2": True},
    }
    candidate = {
        "task-a": {"g::t1": True, "g::t2": True},
        "task-b": {"g::t1": True, "g::t2": True},
    }
    targets = {"task-a": ("t1",), "task-b": ("t1",)}

    report = rederive(stored, candidate, targets)

    assert isinstance(report, SuiteReport)
    assert [verdict.task_id for verdict in report.verdicts] == ["task-a", "task-b"]
    assert report.rate.successes == 2
    assert report.to_json()["tasks"][0]["succeeded"] is True


def test_rederiving_twice_gives_the_same_report():
    stored = {"t": {"g::a": False}}
    candidate = {"t": {"g::a": True}}
    targets = {"t": ("a",)}

    assert rederive(stored, candidate, targets) == rederive(stored, candidate, targets)
