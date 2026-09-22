"""The experience extension keeps memory and published Skill reads session-scoped."""

from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.learning import writeback_disabled
from run_agent_core.messages import AssistantMessage, ToolCall, ToolResultMessage
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_extensions.experience.memory import ENTRY_DELIMITER, MemoryFile

EXTENSION = Path(__file__).resolve().parents[2] / "src" / "run_agent_extensions" / "experience"


def opts(tmp_path, **kwargs):
    return replace(
        options(tmp_path), extension_paths=(EXTENSION,), trust_default="always", **kwargs
    )


def tool(app, name):
    return next(item for item in app.session.tools if item.name == name)


class RecordingProvider(ReplyProvider):
    """Remembers the system prompt each run was actually sent."""

    def __init__(self) -> None:
        self.systems: list[str] = []

    async def stream_response(self, *, messages, system="", **kwargs):
        self.systems.append(system)
        async for event in super().stream_response(messages=messages, **kwargs):
            yield event


async def test_memory_files_are_entry_delimited_and_frozen_in_the_prompt(tmp_path):
    provider = RecordingProvider()
    async with await CodingApplication.open(opts(tmp_path), provider=provider) as app:
        await app.start()
        result = await app.command("/memory add user Use concise replies")
        assert "Added in USER.md" in result.message
        user_file = tmp_path / "state" / "USER.md"
        assert user_file.read_text(encoding="utf-8") == "Use concise replies"

        await app.command("/memory add memory Run pytest before committing")
        project_file = tmp_path / ".run" / "MEMORY.md"
        assert project_file.read_text(encoding="utf-8") == "Run pytest before committing"

        # The prompt shown to the model is the snapshot captured at session start, so
        # a write during the session does not move it until /reload.
        events = [event async for event in app.prompt("hello")]
        assert events[-1].status == "succeeded"
        assert "Use concise replies" not in provider.systems[-1]
        await app.command("/reload")
        _ = [event async for event in app.prompt("again")]
        assert "Use concise replies" in provider.systems[-1]
        assert "Run pytest before committing" in provider.systems[-1]

        await app.command("/memory add user Prefers Chinese")
        assert user_file.read_text(encoding="utf-8") == ENTRY_DELIMITER.join(
            ["Use concise replies", "Prefers Chinese"]
        )
        shown = (await app.command("/memory show")).message
        assert "Prefers Chinese" in shown and "Run pytest" in shown

    other = tmp_path / "other"
    other.mkdir()
    provider = RecordingProvider()
    async with await CodingApplication.open(opts(tmp_path, cwd=other), provider=provider) as app:
        await app.start()
        _ = [event async for event in app.prompt("hi")]
        assert "Use concise replies" in provider.systems[-1]
        assert "Run pytest before committing" not in provider.systems[-1]


async def test_the_memory_tool_adds_replaces_and_removes_single_entries(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        memory = tool(app, "memory")
        added = await memory.execute(
            "1", {"target": "memory", "action": "add", "content": "Build with uv"}
        )
        assert added.details["accepted"] is True and added.details["scope"] == "project"
        duplicate = await memory.execute(
            "2", {"target": "memory", "action": "add", "content": "Build with uv"}
        )
        # A duplicate add is idempotent, the way hermes treats it: reported, not refused.
        assert duplicate.details["accepted"] is True and "already exists" in duplicate.text
        assert (tmp_path / ".run" / "MEMORY.md").read_text(encoding="utf-8") == "Build with uv"
        replaced = await memory.execute(
            "3",
            {
                "target": "memory",
                "action": "replace",
                "old_text": "with uv",
                "new_content": "Build with uv sync",
            },
        )
        assert replaced.details["accepted"] is True
        removed = await memory.execute(
            "4", {"target": "memory", "action": "remove", "old_text": "uv sync"}
        )
        assert removed.details["accepted"] is True
        assert (tmp_path / ".run" / "MEMORY.md").read_text(encoding="utf-8") == ""
        with pytest.raises(ValueError):
            await memory.execute("5", {"target": "memory", "action": "publish"})


def test_a_memory_file_refuses_over_budget_and_ambiguous_writes(tmp_path):
    memory_file = MemoryFile(tmp_path / "MEMORY.md", limit=40)
    memory_file.load()
    assert memory_file.add("alpha fact").accepted
    assert memory_file.add("beta fact").accepted
    refused = memory_file.add("a rather long entry that will not fit")
    assert refused.accepted is False and "exceeds" in refused.message
    assert "alpha fact" in refused.message
    ambiguous = memory_file.replace("fact", "gamma")
    assert ambiguous.accepted is False and "Ambiguous" in ambiguous.message
    assert memory_file.entries == ("alpha fact", "beta fact")
    reloaded = MemoryFile(tmp_path / "MEMORY.md", limit=40)
    reloaded.load()
    assert reloaded.entries == ("alpha fact", "beta fact")


async def test_the_model_can_write_memory_through_a_tool_call(tmp_path):
    class Writer(ReplyProvider):
        async def stream_response(self, *, messages, **kwargs):
            if not any(isinstance(message, ToolResultMessage) for message in messages):
                yield AssistantDoneEvent(
                    reason="toolUse",
                    message=AssistantMessage(
                        content=[
                            ToolCall(
                                id="remember",
                                name="memory",
                                arguments={
                                    "target": "user",
                                    "action": "add",
                                    "content": "Wants answers in Chinese",
                                },
                            ),
                        ],
                        stop_reason="toolUse",
                        model="test",
                    ),
                )
            else:
                async for event in super().stream_response(messages=messages, **kwargs):
                    yield event

    async with await CodingApplication.open(opts(tmp_path), provider=Writer()) as app:
        _ = [event async for event in app.prompt("Remember my language")]
        assert (tmp_path / "state" / "USER.md").read_text(encoding="utf-8") == (
            "Wants answers in Chinese"
        )


async def test_skill_manage_proposes_from_the_last_committed_run_without_publishing(tmp_path):
    content = (
        "---\nname: deploy\ndescription: Deploy safely.\ncreated_by: evolution\n---\n\n"
        "# Deploy\n\n## Procedure\nRun tests.\n"
    )
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        skills = tool(app, "skill_manage")
        arguments = {
            "action": "propose",
            "name": "deploy",
            "operations": [{"action": "add", "new_text": content}],
            "candidate_content": content,
        }
        before_run = await skills.execute("before-run", arguments)
        assert not before_run.details["accepted"]
        assert "committed source run" in before_run.text

        settled = [event async for event in app.prompt("establish evidence")][-1]
        proposed = await skills.execute("after-run", arguments)
        assert proposed.details["accepted"] and proposed.details["status"] == "cold"
        candidate_id = proposed.details["candidate_id"]
        assert settled.run_id in (
            tmp_path / "state" / "experience" / "candidates" / "candidates.jsonl"
        ).read_text(encoding="utf-8")
        assert not (tmp_path / ".run" / "skills" / "deploy" / "SKILL.md").exists()
        listed = (await app.command("/evolve candidates cold")).message
        assert candidate_id in listed and "project/deploy" in listed
        refused = (await app.command(f"/evolve publish {candidate_id}")).message
        assert refused.startswith("Refused:") and "EvaluationService" in refused


async def test_no_memory_is_written_while_writeback_is_off(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        await app.command("/memory add memory normal value")
        with writeback_disabled():
            refused = await tool(app, "memory").execute(
                "1", {"target": "memory", "action": "add", "content": "secret value"}
            )
        assert refused.details["accepted"] is False and "writeback" in refused.text
        assert (tmp_path / ".run" / "MEMORY.md").read_text(encoding="utf-8") == "normal value"


async def test_the_extension_is_optional(tmp_path):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as plain:
        events = [event async for event in plain.prompt("ordinary coding")]
        assert events[-1].status == "succeeded"
        assert all(item.name not in {"memory", "skill_manage"} for item in plain.session.tools)
        assert "Long-term memory" not in provider.systems[-1]
