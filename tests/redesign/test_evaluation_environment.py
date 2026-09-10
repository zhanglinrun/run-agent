"""RED: the five environment elements, reshaped for a coding agent (T-006).

Chapter 7 lists what a repeatable evaluation environment needs - dataset,
resettable state, atomic tools, scoring criteria, and an interaction protocol - and
warns that raising tool abstraction absorbs the planning the evaluation is supposed
to measure: "abstracting too high turns the evaluation into a test of a single
function call, because the tool itself swallows the planning and reasoning".

It also splits environments into human-interaction and tool-calling kinds. A coding
agent is the second kind, so the protocol is not a simulated user running out of
patience; it is the conditions under which the run stops.
"""

from pathlib import Path

import pytest

from run_agent_evals.environment import (
    EvaluationEnvironment,
    NonAtomicTool,
    TerminationPolicy,
    require_atomic_tools,
)
from run_agent_evals.task_spec import load_task_spec

TASKS = Path(__file__).resolve().parents[2] / "evals" / "coding" / "tasks"
MIGRATION = "python-config-migration"


def environment(tmp_path: Path) -> EvaluationEnvironment:
    return EvaluationEnvironment(load_task_spec(TASKS / MIGRATION), tmp_path / "workspace")


def test_the_dataset_is_the_task_and_the_state_starts_pristine(tmp_path):
    env = environment(tmp_path)

    env.reset()

    assert env.dataset.id == MIGRATION
    assert env.workspace.is_dir()
    assert (env.workspace / "settings.py").is_file()
    # The reference is not part of the starting state.
    assert "DEFAULT_TIMEOUT_MS" not in (env.workspace / "defaults.py").read_text()


def test_resetting_returns_to_the_same_initial_state(tmp_path):
    env = environment(tmp_path)
    env.reset()
    before = env.digest()

    (env.workspace / "settings.py").write_text("# vandalised\n", encoding="utf-8")
    (env.workspace / "extra.py").write_text("junk\n", encoding="utf-8")
    env.reset()

    assert env.digest() == before


def test_the_digest_is_not_empty_so_it_cannot_pass_by_comparing_nothing(tmp_path):
    env = environment(tmp_path)
    env.reset()

    assert len(env.digest()) == 64


def test_an_atomic_tool_set_is_accepted():
    require_atomic_tools(["read", "write", "bash"])


def test_a_high_level_tool_is_refused():
    with pytest.raises(NonAtomicTool, match="fix_the_bug"):
        require_atomic_tools(["read", "fix_the_bug"])


def test_each_stop_condition_is_decidable_on_its_own():
    policy = TerminationPolicy(max_turns=3, budget_seconds=10.0)

    assert policy.stop_reason(turns=1, elapsed=1.0) is None
    assert policy.stop_reason(turns=3, elapsed=1.0) == "turn limit"
    assert policy.stop_reason(turns=1, elapsed=11.0) == "budget exhausted"
    assert policy.stop_reason(turns=1, elapsed=1.0, cancelled=True) == "cancelled"
    assert policy.stop_reason(turns=1, elapsed=1.0, finished=True) == "completed"
