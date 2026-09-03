from __future__ import annotations

from pathlib import Path

import pytest

from agents import Agent
from agents.harness import TaskResult, TaskStatus, Usage


@pytest.mark.asyncio
async def test_public_agent_is_a_thin_harness_facade(tmp_path: Path) -> None:
    agent = Agent(
        api_key="test-key",
        model="test-model",
        workspace=tmp_path,
        session_db=tmp_path / "sessions.db",
        persist_session=True,
        disable_extensions=("memory", "skills", "verification", "correction"),
    )
    captured = []

    async def fake_run(task):
        captured.append(task)
        return TaskResult(
            task.task_id,
            TaskStatus.COMPLETED,
            answer="ok",
            usage=Usage(input_tokens=2, output_tokens=1),
            session_id="session-1",
        )

    agent.harness.run = fake_run
    result = await agent.run_once("hello")

    assert result.answer == "ok"
    assert agent.session_id == "session-1"
    assert captured[0].mode == "interactive"
    assert "verification" in captured[0].runtime.extensions.disabled
    assert "correction" in captured[0].runtime.extensions.disabled
    assert captured[0].runtime.provider.api_key == "test-key"
    assert captured[0].metadata == {}
    await agent.close()


@pytest.mark.asyncio
async def test_public_agent_uses_sqlite_sessions(tmp_path: Path) -> None:
    agent = Agent(api_key="test-key", workspace=tmp_path, session_db=tmp_path / "sessions.db")
    assert agent.list_sessions() == []
    await agent.close()
