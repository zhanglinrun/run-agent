import os
import subprocess
import sys
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_coding.skills import load_skills
from run_agent_coding.storage.skill_packages import (
    ArtifactCorrupt,
    SkillPackageError,
    SkillPackageStore,
)
from run_agent_core.session.contracts import SessionConflict


class RecordingProvider(ReplyProvider):
    def __init__(self):
        self.requests = []

    async def stream_response(self, *, model, system, messages, tools, **kwargs):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                        "execution_mode": tool.execution_mode,
                    }
                    for tool in tools
                ],
            }
        )
        async for event in super().stream_response(messages=messages, **kwargs):
            yield event


def resource_events(app):
    return [
        entry for entry in app.session._state.custom_entries if entry.namespace == "run.resources"
    ]


@pytest.fixture
def skill_root(tmp_path):
    root = options(tmp_path).paths.home / "skills" / "example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\ndescription: stable v1\n---\nUse refs/note.md\n", encoding="utf-8"
    )
    (root / "refs").mkdir()
    (root / "refs/note.md").write_text("reference v1", encoding="utf-8")
    (root / "helper.py").write_text("print('script v1')", encoding="utf-8")
    return root


async def test_skill_body_references_and_script_stay_fixed_until_reload(tmp_path, skill_root):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        skill = app.session.skills[0]
        assert skill.path != skill_root / "SKILL.md"
        assert skill.package_digest in str(skill.path)
        (skill_root / "SKILL.md").write_text(
            "---\ndescription: v2\n---\nUse the new body", encoding="utf-8"
        )
        (skill_root / "refs/note.md").write_text("reference v2", encoding="utf-8")
        (skill_root / "helper.py").write_text("print('script v2')", encoding="utf-8")
        _ = [event async for event in app.prompt("/skill:example use it")]
        assert "Use refs/note.md" in str(provider.requests)
        assert (skill.path.parent / "refs/note.md").read_text() == "reference v1"
        result = subprocess.run(
            [sys.executable, str(skill.path.parent / "helper.py")],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "script v1"
        await app.command("/reload")
        updated = app.session.skills[0]
        assert updated.package_digest != skill.package_digest
        assert updated.content == "Use the new body"
        assert (updated.path.parent / "refs/note.md").read_text() == "reference v2"
        assert (skill.path.parent / "refs/note.md").read_text() == "reference v1"


async def test_cache_tampering_blocks_model_input(tmp_path, skill_root):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        skill = app.session.skills[0]
        ref = skill.path.parent / "refs/note.md"
        ref.chmod(0o600)
        ref.write_text("unexpected change", encoding="utf-8")
        with pytest.raises(ArtifactCorrupt, match="modified"):
            _ = [event async for event in app.prompt("do not use changed resources")]
        assert provider.requests == []
        store = app.session._config.skill_packages
        with pytest.raises(ArtifactCorrupt):
            await store.restore("example", skill.package_digest)


async def test_package_file_limit_is_explicit(tmp_path, skill_root):
    (skill_root / "oversize.bin").write_bytes(b"x" * (16 * 1024 * 1024))
    store = SkillPackageStore(tmp_path / "cache")
    skill = load_skills(
        RunAgentResourcePaths(root=options(tmp_path).paths.home, agents_root=None)
    )[0]
    with pytest.raises(SkillPackageError, match="16 MiB"):
        await store.freeze(skill)
    assert not any(tmp_path.joinpath("cache").rglob("SKILL.md"))


async def test_file_link_cannot_escape_a_frozen_package(tmp_path, skill_root):
    outside = tmp_path / "outside.txt"
    outside.write_text("live", encoding="utf-8")
    try:
        os.symlink(outside, skill_root / "external.txt")
    except OSError:
        pytest.skip("This host cannot create symlinks")
    with pytest.raises(SkillPackageError, match="file link"):
        await CodingApplication.open(options(tmp_path), provider=RecordingProvider())


async def test_branch_and_restart_restore_recorded_skill_and_system_versions(tmp_path, skill_root):
    opts = options(tmp_path)
    system = opts.paths.home / "SYSTEM.md"
    system.write_text("System version one", encoding="utf-8")
    async with await CodingApplication.open(opts, provider=RecordingProvider()) as app:
        events = [event async for event in app.prompt("first task")]
        original_head = events[-1].head_id
        session_id = app.session.session_id
        original = app.session.skills[0].package_digest
        first_resource = resource_events(app)[0].id
        snapshot = await app.session.storage.get_snapshot(events[-1].snapshot_id)
        assert snapshot["payload"]["resource_snapshot_id"] == first_resource
        system.write_text("System version two", encoding="utf-8")
        (skill_root / "SKILL.md").write_text("Replacement skill body", encoding="utf-8")
        await app.command("/reload")
        assert app.session.skills[0].package_digest != original
        assert "System version two" in app.session._harness.config.system
        _ = [event async for event in app.prompt("second task")]
        await app.session.branch_to_entry(original_head)
        assert app.session.skills[0].package_digest == original
        assert "System version one" in app.session._harness.config.system
        assert [entry.data["reason"] for entry in resource_events(app)] == ["startup", "branch"]
    skill_root.rename(skill_root.with_name("moved-original"))
    system.write_text("System version three", encoding="utf-8")
    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=RecordingProvider()
    ) as app:
        await app.start()
        assert app.session.skills[0].package_digest == original
        assert "System version one" in app.session._harness.config.system
        assert [entry.data["reason"] for entry in resource_events(app)] == [
            "startup",
            "branch",
            "resume",
        ]


async def test_resource_marker_and_extension_bindings_roll_back_together(
    tmp_path, skill_root, monkeypatch
):
    extension = tmp_path / "extension.py"
    extension.write_text("def setup(api): pass", encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=RecordingProvider()) as app:
        await app.start()
        old_runtime = app.session.extension_runtime
        original = app.session._prepare_resource_activation
        old_marker = resource_events(app)[-1].id
        old_skill = app.session.skills[0].package_digest

        async def conflicting(*args, **kwargs):
            activation = await original(*args, **kwargs)
            return replace(activation, expected_head="concurrent-head")

        monkeypatch.setattr(app.session, "_prepare_resource_activation", conflicting)
        (skill_root / "SKILL.md").write_text("next version", encoding="utf-8")
        with pytest.raises(SessionConflict):
            await app.command("/reload")
        assert app.session.extension_runtime is old_runtime and old_runtime.active
        assert app.session.skills[0].package_digest == old_skill
        assert resource_events(app)[-1].id == old_marker
        assert (await app.session.storage.get_head()).entry_id == old_marker
