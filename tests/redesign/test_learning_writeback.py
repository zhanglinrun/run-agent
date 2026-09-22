"""A measured evaluation must not write experience back.

Plan 6.6 requires that the evaluation process has learning writeback switched off.
Holding that only because the bench path happens not to load the extension is not
a guard - it is a coincidence. These tests pin the guard: a learning write is
refused while writeback is off, and the evaluation executor switches it off for
the duration of a trial and restores it afterwards.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_evaluation_sqlite import EvaluatedProvider, settings

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.learning import (
    LearningWritebackDisabled,
    require_writeback,
    writeback_disabled,
    writeback_enabled,
)
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import FrozenTask

REPO = Path(__file__).resolve().parents[2]
MEMORY = REPO / "src" / "run_agent_extensions" / "hermes_memory"


def memory_options(tmp_path):
    return replace(options(tmp_path), extension_paths=(MEMORY,), extensions_enabled=True)


def test_writeback_is_enabled_unless_switched_off() -> None:
    assert writeback_enabled() is True
    with writeback_disabled():
        assert writeback_enabled() is False
        with pytest.raises(LearningWritebackDisabled):
            require_writeback()
    assert writeback_enabled() is True


async def test_no_memory_is_written_while_writeback_is_off(tmp_path):
    async with await CodingApplication.open(
        memory_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        await app.command("/memory add memory normal value")
        memory_file = tmp_path / ".run" / "MEMORY.md"
        before = memory_file.read_text(encoding="utf-8")

        # The command path surfaces the refusal rather than raising it, so assert the
        # effect: with writeback off the file is untouched.
        with writeback_disabled():
            await app.command("/memory add memory secret value")
        after = memory_file.read_text(encoding="utf-8")

        assert after == before
        assert "secret value" not in after, after
        assert "normal value" in after, after


async def test_the_evaluator_disables_writeback_for_the_duration_of_a_trial(tmp_path, monkeypatch):
    import run_agent_evals.coding as coding

    observed = []
    original_open = coding.CodingApplication.open

    async def recording_open(*args, **kwargs):
        observed.append(writeback_enabled())
        return await original_open(*args, **kwargs)

    monkeypatch.setattr(coding.CodingApplication, "open", recording_open)
    monkeypatch.setattr(
        "run_agent_coding.session.create_model_provider", lambda *a, **k: EvaluatedProvider()
    )
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = FrozenTask("case", fixture, "answer", ((sys.executable, "-c", "pass"),))
    executor = CodingTaskExecutor(tmp_path / "state", provider_settings=settings())
    await executor.execute(task, workspace)

    assert observed == [False], "the trial must run with writeback off"
    assert writeback_enabled() is True, "and be restored afterwards"
