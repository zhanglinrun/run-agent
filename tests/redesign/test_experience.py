"""The experience extension over a real session: Markdown memory and managed Skills.

Memory follows my-pi-agent: two entry-delimited files with a character budget, a
prompt snapshot that is frozen for the life of the session, and a controlled tool
that adds, replaces and removes single entries. Skills follow hermes-agent's
``skill_manage``: a SKILL.md directory the ordinary loader picks up on reload.
"""

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


async def test_skill_manage_writes_a_loadable_skill_and_refuses_bad_paths(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        skills = tool(app, "skill_manage")
        created = await skills.execute(
            "1",
            {
                "action": "create",
                "name": "deploy",
                "description": "Deploy the service safely",
                "body": "# Deploy\n\n## Procedure\n1. Run the tests.\n",
            },
        )
        assert created.details["accepted"] is True
        skill_file = tmp_path / ".run" / "skills" / "deploy" / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        assert "description: Deploy the service safely" in text
        assert "created_by: agent" in text

        assert "deploy" not in app.session.system_prompt
        await app.command("/reload")
        assert "deploy" in app.session.system_prompt
        assert "Deploy the service safely" in app.session.system_prompt

        patched = await skills.execute(
            "2",
            {
                "action": "patch",
                "name": "deploy",
                "old_text": "1. Run the tests.",
                "new_text": "1. Run the tests.\n2. Tag the release.",
            },
        )
        assert patched.details["accepted"] is True
        assert "Tag the release" in skill_file.read_text(encoding="utf-8")

        written = await skills.execute(
            "3",
            {
                "action": "write_file",
                "name": "deploy",
                "file_path": "references/checklist.md",
                "content": "- verify\n",
            },
        )
        assert written.details["accepted"] is True
        escaped = await skills.execute(
            "4",
            {"action": "write_file", "name": "deploy", "file_path": "../evil.md", "content": "x"},
        )
        assert escaped.details["accepted"] is False
        viewed = await skills.execute("5", {"action": "view", "name": "deploy"})
        assert "Tag the release" in viewed.text
        deleted = await skills.execute("6", {"action": "delete", "name": "deploy"})
        assert deleted.details["accepted"] is True and not skill_file.exists()


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
