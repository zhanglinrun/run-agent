"""The experience extension is verifier-gated Skill evolution only.

Memory was moved to the ``hermes_memory`` package: nothing here may register a
``memory`` tool or a ``/memory`` command any more. The memory invariants themselves
(snapshot freezing, threat masking, budgets, drift) live in the
``test_hermes_memory_*`` files.
"""

from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication

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


async def test_the_experience_extension_registers_skills_but_not_memory(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        names = {item.name for item in app.session.tools}
        assert "skill_manage" in names
        assert "memory" not in names
        assert (await app.command("/evolve status")).handled is True
        assert (await app.command("/memory show")).handled is False
        # The prompt guideline no longer claims long-term memory.
        guidelines = app.session.extension_runtime.prompt_guidelines
        assert all("Long-term memory" not in guideline for guideline in guidelines)


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


async def test_the_extension_is_optional(tmp_path):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as plain:
        events = [event async for event in plain.prompt("ordinary coding")]
        assert events[-1].status == "succeeded"
        assert all(item.name not in {"memory", "skill_manage"} for item in plain.session.tools)
        assert "Long-term memory" not in provider.systems[-1]
