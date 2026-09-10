import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from extensions.experience.models import Proposal
from extensions.experience.repository import ExperienceRepository
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_core.messages import AssistantMessage, ToolCall, ToolResultMessage
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.contracts import SessionConflict

EXTENSION = Path(__file__).resolve().parents[2] / "extensions" / "experience"


def opts(tmp_path, **kwargs):
    return replace(options(tmp_path), extension_paths=(EXTENSION,), **kwargs)


def repository(app):
    return ExperienceRepository(context(app).services, app.session.session_id)


async def evidence(app, action="remember"):
    return await app.session.append_custom_entry(
        "experience.command",
        {
            "action": action,
            "scope": "project",
            "arguments": [],
        },
    )


async def test_manual_preference_persists_scopes_refresh_forget_and_rollback(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        result = await app.command('/experience remember user user style "Use concise replies"')
        assert "promoted" in result.message
        original = (await repository(app).search("user"))[0]
        assert "Use concise replies" not in app.session.system_prompt
        await app.command("/reload")
        assert "Use concise replies" in app.session.system_prompt
        await app.command('/experience remember project memory build "Run pytest"')
        await app.command("/reload")
        assert "Run pytest" in app.session.system_prompt
        session_id = app.session.session_id
        await app.command("/experience forget user user style")
        await app.command("/reload")
        assert "Use concise replies" not in app.session.system_prompt
        await app.command(f"/experience rollback user user/style {original.version}")
        assert (await repository(app).search("user"))[0].version == original.version
    async with await CodingApplication.open(
        opts(tmp_path, resume=session_id, refresh_resources=True), provider=ReplyProvider()
    ) as resumed:
        await resumed.start()
        assert "Use concise replies" in resumed.session.system_prompt
    other = tmp_path / "other"
    other.mkdir()
    async with await CodingApplication.open(
        opts(tmp_path, cwd=other), provider=ReplyProvider()
    ) as app:
        await app.start()
        assert "Use concise replies" in app.session.system_prompt
        assert "Run pytest" not in app.session.system_prompt


async def test_candidates_do_not_publish_and_concurrent_heads_do_not_lose_updates(tmp_path):
    async with (
        await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as first,
        await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as second,
    ):
        await first.start()
        await second.start()
        a, b = await asyncio.gather(
            repository(first).propose(
                Proposal(name="build", content="first"), command_id=await evidence(first)
            ),
            repository(second).propose(
                Proposal(name="build", content="second"), command_id=await evidence(second)
            ),
        )
        assert not await repository(first).search("project")
        results = await asyncio.gather(
            repository(first).publish_manual("project", a.candidate_id, await evidence(first)),
            repository(second).publish_manual("project", b.candidate_id, await evidence(second)),
            return_exceptions=True,
        )
        assert sum(isinstance(item, SessionConflict) for item in results) == 1
        assert len(await repository(first).search("project")) == 1
        assert len(await repository(first).candidates("project")) == 2


async def test_model_tool_can_only_propose_with_actual_snapshot_evidence(tmp_path):
    class Proposer(ReplyProvider):
        async def stream_response(self, *, messages, **kwargs):
            if not any(isinstance(message, ToolResultMessage) for message in messages):
                yield AssistantDoneEvent(
                    reason="toolUse",
                    message=AssistantMessage(
                        content=[
                            ToolCall(
                                id="propose",
                                name="memory",
                                arguments={
                                    "action": "propose",
                                    "proposal": {
                                        "name": "observed",
                                        "content": "An observed project constraint",
                                    },
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

    async with await CodingApplication.open(opts(tmp_path), provider=Proposer()) as app:
        _ = [event async for event in app.prompt("Investigate the project")]
        candidates = await repository(app).candidates("project")
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.status == "needs_evidence" and candidate.source_kind == "model"
        snapshot = await context(app).services.snapshots.read(candidate.source_snapshot)
        assert snapshot.run_id == candidate.source_run
        assert not await repository(app).search("project")
        result = await app.command(f"/experience publish project {candidate.candidate_id}")
        assert "promoted manually" in result.message
        assert len(await repository(app).search("project")) == 1


async def test_source_proof_required_and_tool_rejects_publication_action(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        with pytest.raises(ValueError, match="source command"):
            await repository(app).propose(Proposal(name="no-evidence", content="unverified"))
        tool = next(item for item in app.session.tools if item.name == "memory")
        with pytest.raises(ValueError):
            await tool.execute("publish", {"action": "publish", "scope": "project"})
        with pytest.raises(ValueError, match="No recorded model input"):
            await tool.execute(
                "propose",
                {
                    "action": "propose",
                    "proposal": {"name": "test", "content": "unverified"},
                },
            )
        await app.command('/experience remember project memory search "deterministic evidence"')
        result = await tool.execute("search", {"action": "search", "query": "deterministic"})
        assert json.loads(result.text)[0]["key"] == "memory/search"


async def test_expired_resources_are_omitted_from_new_snapshots_and_extension_is_optional(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.start()
        repo = repository(app)
        candidate = await repo.propose(
            Proposal(name="old", content="expired environment", expires_at=1),
            command_id=await evidence(app),
        )
        await repo.publish_manual("project", candidate.candidate_id, await evidence(app))
        await app.command("/reload")
        assert "expired environment" not in app.session.system_prompt
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as plain:
        events = [event async for event in plain.prompt("ordinary coding")]
        assert events[-1].status == "succeeded"
        assert all(tool.name != "memory" for tool in plain.session.tools)


async def test_markdown_checkout_preserves_edits_and_import_checks_base_version(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        await app.command('/experience remember project memory build "Original build command"')
        version = (await repository(app).search("project"))[0].version
        result = await app.command("/experience checkout project memory build")
        path = Path(result.message)
        assert path.name == "MEMORY.md" and path.read_text() == "Original build command"
        path.write_text("Manually corrected build command", encoding="utf-8")
        await app.command("/experience checkout project memory build")
        assert path.read_text() == "Manually corrected build command"
        candidate = await app.command(f"/experience import project memory build {version}")
        candidate_id = candidate.message.split()[0]
        await app.command(f"/experience publish project {candidate_id}")
        assert (await repository(app).search("project"))[0].content == path.read_text()
        conflict = await app.command(f"/experience import project memory build {version}")
        assert "base changed" in conflict.message
        newer = Path((await app.command("/experience checkout project memory build")).message)
        assert newer != path and path.read_text() == "Manually corrected build command"


async def test_skill_index_and_lazy_body_stay_on_the_same_version(tmp_path):
    async with await CodingApplication.open(opts(tmp_path), provider=ReplyProvider()) as app:
        created = await app.command(
            '/experience propose project skill build "Inspect configuration before testing"'
        )
        await app.command(f"/experience publish project {created.message.split()[0]}")
        await app.command("/reload")
        assert "project/skill/build" in app.session.system_prompt
        assert "Inspect configuration before testing" not in app.session.system_prompt
        tool = next(item for item in app.session.tools if item.name == "experience_skill")
        first = await tool.execute("skill", {"name": "build"})
        assert first.text == "Inspect configuration before testing"
        newer = await app.command('/experience propose project skill build "Different procedure"')
        await app.command(f"/experience publish project {newer.message.split()[0]}")
        assert (await tool.execute("skill", {"name": "build"})).text == first.text
        await app.command("/reload")
        tool = next(item for item in app.session.tools if item.name == "experience_skill")
        assert (await tool.execute("skill", {"name": "build"})).text == "Different procedure"
