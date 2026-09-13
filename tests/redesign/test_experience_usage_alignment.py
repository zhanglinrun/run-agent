"""Usage attribution follows the actual selected and successfully read Skill."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.redesign.test_coding_application import options
from tests.redesign.test_experience_curator import BODY
from tests.redesign.test_experience_review_wiring import EXPERIENCE
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionContext
from run_agent_coding.host.learning import review_origin, writeback_disabled
from run_agent_core.messages import AssistantMessage, TextContent, ToolCall
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_extensions.experience.config import ExperienceConfig
from run_agent_extensions.experience.curator import Curator
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


class ReadingProvider:
    def __init__(self):
        self.paths = []
        self.index = 0

    async def stream_response(self, *, system, **kwargs):
        if "session name" not in system.lower() and self.index < len(self.paths):
            path = self.paths[self.index]
            self.index += 1
            content = [
                ToolCall(id=f"read-{self.index}", name="read", arguments={"path": str(path)})
            ]
            reason = "toolUse"
        else:
            content = [TextContent(text="done")]
            reason = "stop"
        yield AssistantDoneEvent(
            reason=reason,
            message=AssistantMessage(
                content=content, model="test", provider="test", stop_reason=reason
            ),
        )


def configured(tmp_path):
    opts = replace(
        options(tmp_path),
        extension_paths=(EXPERIENCE,),
        extensions_enabled=True,
        trust_override="approve",
    )
    manager = SkillManager(
        SkillRoots(opts.paths.user_skills_dir, opts.paths.project_skills_dir(tmp_path))
    )
    for scope in ("user", "project"):
        path = manager.roots.directory(scope) / "deploy" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: deploy\ndescription: Deploy safely.\n---\n" + BODY, encoding="utf-8"
        )
    return opts, manager


async def test_selected_scope_and_successful_cached_reads_count_once_per_run(tmp_path):
    opts, manager = configured(tmp_path)
    provider = ReadingProvider()
    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        skill = context(app).skills[0]
        assert skill.source_path == manager.roots.project / "deploy" / "SKILL.md"
        assert skill.path != skill.source_path and skill.package_digest
        provider.paths = [skill.path, skill.path]
        events = [event async for event in app.prompt("/skill:deploy check this")]
        assert events[-1].status == "succeeded"
        assert manager.usage["project"].get("deploy")["view_count"] == 2
        assert manager.usage["project"].get("deploy")["use_count"] == 1
        assert manager.usage["user"].get("deploy")["use_count"] == 0
        assert manager.usage["project"].get("deploy")["created_by"] is None
        provider.index = 0
        await anext_run(app, "consult automatically again")
        assert manager.usage["project"].get("deploy")["use_count"] == 2


async def anext_run(app, prompt):
    return [event async for event in app.prompt(prompt)]


@pytest.mark.parametrize("mode", ["failed", "review", "evaluation"])
async def test_failed_or_maintenance_reads_do_not_count(tmp_path, mode):
    opts, manager = configured(tmp_path)
    provider = ReadingProvider()
    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        skill = context(app).skills[0]
        provider.paths = [skill.source_path if mode == "failed" else skill.path]
        if mode == "failed":
            skill.source_path.unlink()
            events = await anext_run(app, "read skill")
        elif mode == "review":
            with review_origin():
                events = await anext_run(app, "read skill")
        else:
            with writeback_disabled():
                events = await anext_run(app, "read skill")
        reads = [event for event in events if event.type == "tool_execution_end"]
        assert len(reads) == 1
        assert reads[0].is_error == (mode == "failed")
        row = manager.usage["project"].get("deploy")
        assert row["view_count"] == row["use_count"] == 0


async def test_old_frozen_package_does_not_credit_reuse_of_new_support_patch(tmp_path):
    opts, manager = configured(tmp_path)
    manager.write_file("project", "deploy", "references/check.md", "old")
    usage = manager.usage["project"]
    usage.bump_use("deploy")
    provider = ReadingProvider()
    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        skill = context(app).skills[0]
        manager.write_file("project", "deploy", "references/check.md", "new")
        generation = usage.get("deploy")["patch_generation"]
        provider.paths = [skill.path]
        await anext_run(app, "consult the frozen skill")
        row = usage.get("deploy")
        assert row["use_count"] == 2
        assert row["last_reused_patch_generation"] < generation
        assert skill == context(app).skills[0]
        await app.command("/reload")
        provider.paths = [context(app).skills[0].path]
        provider.index = 0
        await anext_run(app, "consult the refreshed skill")
        assert usage.get("deploy")["last_reused_patch_generation"] == generation


def test_restoration_refreshes_inactivity_without_claiming_use_or_ownership(tmp_path):
    _, manager = configured(tmp_path)
    usage = manager.usage["project"]
    usage.adopt("deploy")
    ancient = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    usage._mutate("deploy", lambda row: row.update(created_at=ancient))
    curator = Curator(manager, ExperienceConfig(), tmp_path / "curator")
    assert curator.apply_transitions()["archived"] == 1
    assert usage.restore("deploy")[0]
    assert curator.apply_transitions()["archived"] == 0
    row = usage.get("deploy")
    assert row["state"] == "active" and row["last_restored_at"]
    assert row["use_count"] == row["view_count"] == 0
    assert row["created_by"] == "agent"


def test_custom_session_without_skills_keeps_empty_public_metadata():
    assert ExtensionContext(SimpleNamespace(session_view=SimpleNamespace())).skills == ()
