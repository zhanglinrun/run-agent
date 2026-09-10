"""Provider selections travel through actual activation, resume and Gateway paths."""

import asyncio
import os
import py_compile
from dataclasses import replace

import pytest
from tests.redesign.git_helpers import create_repository
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_context_snapshots import RecordingProvider
from tests.redesign.test_gateway_runtime import runtime, submit
from tests.redesign.test_host_services import context
from tests.redesign.test_skill_packages import resource_events

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionError
from run_agent_coding.extensions.loader import load_extensions
from run_agent_coding.host.contracts import HeadChange, StateChange
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_coding.session_manager import SessionManager
from run_agent_gateway.coding import CodingAssignmentRunner

__all__ = ["runtime"]


@pytest.fixture
def resource_extension(tmp_path):
    extension = tmp_path / "resource_provider.py"
    extension.write_text(
        """
from run_agent_coding.extensions import ResourceSelection

def setup(api):
    def select(view):
        try:
            api.context.services
        except RuntimeError:
            pass
        else:
            raise AssertionError("Staged providers must not have writable services")
        result = []
        for scope in ("session", "project", "user"):
            for key, version in view.heads(scope).items():
                value = view.resolve(scope, key, version)
                value.metadata["title"] = "mutation of a detached copy"
                result.append(ResourceSelection(scope, key, version, key))
        return result
    api.register_resource_provider("notes", select, version="1")
""",
        encoding="utf-8",
    )
    return extension


async def publish(app, text, *, scope="project", key="MEMORY.md"):
    resources = context(app).services.scope(scope).resources
    heads = await resources.snapshot()
    value = await resources.put_immutable(
        key,
        text,
        parent_version=heads.get(key),
        metadata={"title": key},
    )
    await resources.advance_head(HeadChange(key, heads.get(key), value.version, "test", {}))
    return value.version


async def test_explicit_refresh_resume_and_branch_use_exact_versions(tmp_path, resource_extension):
    opts = replace(options(tmp_path), extension_paths=(resource_extension,))
    provider = RecordingProvider()
    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        session_id = app.session.session_id
        await publish(app, "memory version one")
        assert "memory version one" not in app.session.system_prompt
        await app.command("/reload")
        assert "memory version one" in app.session.system_prompt
        first = [event async for event in app.prompt("first task")][-1]
        snapshot = await app.session.storage.repository.get_snapshot(first.snapshot_id)
        resource = snapshot["payload"]["resource_inputs"]["extension_resources"]
        assert resource["contributions"][0]["resource"]["metadata"]["title"] == "MEMORY.md"
        await publish(app, "memory version two")
        _ = [event async for event in app.prompt("still pinned")]
        assert "memory version one" in provider.requests[-1]["system"]
        assert "memory version two" not in provider.requests[-1]["system"]
        await app.command("/reload")
        assert "memory version two" in app.session.system_prompt
        await app.session.branch_to_entry(first.head_id)
        assert "memory version one" in app.session.system_prompt
        assert "memory version two" not in app.session.system_prompt

    # Resumption must not even invoke a selector against live heads.
    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=provider
    ) as reopened:

        async def forbidden(*args):
            raise AssertionError("Resume must not capture latest resource heads")

        reopened.session.host_services.capture_resources = forbidden
        await reopened.start()
        assert "memory version one" in reopened.session.system_prompt
        _ = [event async for event in reopened.prompt("resumed task")]
        assert "memory version one" in provider.requests[-1]["system"]


async def test_capture_is_consistent_across_scopes_and_sources(tmp_path, resource_extension):
    other_extension = tmp_path / "other.py"
    other_extension.write_text(resource_extension.read_text(), encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(resource_extension, other_extension))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as first:
        await first.start()
        await publish(first, "session-only", scope="session", key="local")
        await publish(first, "project-only", key="project")
        await publish(first, "user-wide", scope="user", key="user")
        runtime = first.session.extension_runtime
        sources = tuple(item.source_id for item in runtime._extensions)
        views = await first.session.host_services.capture_resources(
            first.session.session_id,
            sources,
            runtime._generation.assert_active,
        )
        await publish(first, "project-new", key="project")
        frozen = views[sources[0]]
        version = frozen.heads("project")["project"]
        assert frozen.resolve("project", "project", version).content == "project-only"
        assert not views[sources[1]].heads("user")
        with pytest.raises(KeyError, match="captured heads"):
            frozen.resolve("user", "project", version)
        other = tmp_path / "different-project"
        other.mkdir()
        async with await CodingApplication.open(
            replace(opts, cwd=other), provider=ReplyProvider()
        ) as second:
            await second.start()
            assert "user-wide" in second.session.system_prompt
            assert "project-only" not in second.session.system_prompt
            assert "session-only" not in second.session.system_prompt
        manager = SessionManager(opts.paths, principal_id="different-user")
        try:
            async with await CodingApplication.open(
                opts, provider=ReplyProvider(), manager=manager
            ) as third:
                await third.start()
                assert "user-wide" not in third.session.system_prompt
        finally:
            await manager.aclose()


async def test_budget_failure_keeps_previous_runtime_and_resource_marker(
    tmp_path, resource_extension
):
    opts = replace(options(tmp_path), extension_paths=(resource_extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        await publish(app, "valid memory")
        await app.command("/reload")
        old_runtime = app.session.extension_runtime
        old_marker = resource_events(app)[-1].id
        old_system = app.session.system_prompt
        await publish(app, "x" * 40000)
        with pytest.raises(ValueError, match="context budget"):
            await app.command("/reload")
        assert app.session.extension_runtime is old_runtime and old_runtime.active
        assert resource_events(app)[-1].id == old_marker
        assert app.session.system_prompt == old_system
        await context(app).services.scope().state.compare_and_set(StateChange("alive", 0, True))


async def test_activation_conflict_rolls_back_selected_content(
    tmp_path, resource_extension, monkeypatch
):
    opts = replace(options(tmp_path), extension_paths=(resource_extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        old = app.session.extension_runtime
        marker = resource_events(app)[-1].id
        await publish(app, "uncommitted context")

        async def fail(*args, **kwargs):
            raise OSError("publication failure")

        monkeypatch.setattr(app.session.host_services, "publish", fail)
        with pytest.raises(OSError, match="publication failure"):
            await app.command("/reload")
        assert app.session.extension_runtime is old and old.active
        assert "uncommitted context" not in app.session.system_prompt
        assert resource_events(app)[-1].id == marker


async def test_changed_provider_contract_rejects_resume_and_stale_api_cannot_register(
    tmp_path, resource_extension
):
    opts = replace(options(tmp_path), extension_paths=(resource_extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        session_id = app.session.session_id
        api = app.session.extension_runtime._extensions[0].api
        with pytest.raises(ExtensionError, match="during setup"):
            api.register_resource_provider("late", lambda _: [], version="1")
        await app.command("/reload")
        with pytest.raises(ExtensionError, match="stale"):
            api.register_resource_provider("late", lambda _: [], version="1")
    resource_extension.write_text(
        resource_extension.read_text().replace('version="1"', 'version="changed-version"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="providers differ"):
        await CodingApplication.open(replace(opts, resume=session_id), provider=ReplyProvider())


async def test_setup_failure_removes_provider_and_selector_exception_aborts_activation(tmp_path):
    extension = tmp_path / "bad.py"
    extension.write_text(
        """
def setup(api):
    def select(view):
        raise ValueError("selector failed")
    api.register_resource_provider("bad", select, version="1")
    raise ValueError("setup failed")
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        assert not app.session.extension_runtime.context_resources.registrations
    extension.write_text(
        extension.read_text().replace('    raise ValueError("setup failed")', ""),
        encoding="utf-8",
    )
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        with pytest.raises(ValueError, match="selector failed"):
            await app.start()
        assert not resource_events(app)
        with pytest.raises(ExtensionError, match="after session activation"):
            _ = context(app).services


async def test_background_uses_source_resource_content_after_live_head_changes(
    runtime, tmp_path, resource_extension
):
    repo, owner, host = runtime
    workspace = create_repository(tmp_path / "project")
    host.options = replace(host.options, extension_paths=(resource_extension,))
    manager = SessionManager(host.options.paths, database=repo.database, principal_id="alice")
    async with await CodingApplication.open(
        replace(host.options, cwd=workspace), provider=ReplyProvider(), manager=manager
    ) as seed:
        await seed.start()
        await publish(seed, "fixed gateway memory", scope="user")
    first = await repo.admit(owner, submit(workspace), model="test")
    runner = CodingAssignmentRunner(host)
    foreground = await repo.claim_next(owner)
    await runner.run(foreground, asyncio.Event())
    await repo.release(owner, foreground)
    background = await repo.admit(
        owner,
        replace(submit(workspace, "background", "isolated task"), lane="background"),
        model="test",
    )
    async with await CodingApplication.open(
        replace(host.options, cwd=workspace), provider=ReplyProvider(), manager=manager
    ) as updater:
        await updater.start()
        await publish(updater, "new live gateway memory", scope="user")
    await manager.aclose()
    provider = RecordingProvider()
    host.provider_factory = lambda _: provider
    assignment = await repo.claim_next(owner)
    await runner.run(assignment, asyncio.Event())
    await repo.release(owner, assignment)
    task = await repo.task(background.task_id, principal_id="alice")
    assert task["status"] == "succeeded", task
    assert first.session_id != assignment.session_id
    assert "fixed gateway memory" in provider.requests[-1]["system"]
    assert "new live gateway memory" not in provider.requests[-1]["system"]


async def test_changed_extension_code_blocks_model_and_resume_until_explicit_reload(
    tmp_path, resource_extension
):
    opts = replace(options(tmp_path), extension_paths=(resource_extension,))
    provider = RecordingProvider()
    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        session_id = app.session.session_id
        resource_extension.write_text(
            resource_extension.read_text() + "\n# changed implementation\n",
            encoding="utf-8",
        )
        with pytest.raises(ExtensionError, match="source changed"):
            _ = [event async for event in app.prompt("must not call model")]
        assert not provider.requests
    with pytest.raises(ValueError, match="implementation differs"):
        await CodingApplication.open(replace(opts, resume=session_id), provider=provider)
    async with await CodingApplication.open(
        replace(opts, resume=session_id, refresh_resources=True), provider=provider
    ) as refreshed:
        await refreshed.start()
        assert resource_events(refreshed)[-1].data["reason"] == "refresh"
        _ = [event async for event in refreshed.prompt("explicit new resource version")]
        assert provider.requests
    with pytest.raises(ValueError, match="cannot be refreshed"):
        await CodingApplication.open(
            replace(opts, resume=session_id, pinned_resources=True, refresh_resources=True),
            provider=provider,
        )


def test_reload_executes_current_package_sources_even_with_valid_old_pyc(tmp_path):
    package = tmp_path / "extension_package"
    package.mkdir()
    entry = package / "extension.py"
    helper = package / "helper.py"
    entry.write_text("from .helper import value\ndef setup(api): api.append(value)\n")
    helper.write_text('value = "old"\n')
    for path in (entry, helper):
        py_compile.compile(str(path), doraise=True)
    stamp = helper.stat()
    helper.write_text('value = "new"\n')
    os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    result = load_extensions(
        RunAgentResourcePaths(root=tmp_path),
        extra_paths=(package,),
        include_resource_dirs=False,
    )
    assert not result.diagnostics
    actual = []
    result.extensions[0].setup(actual)
    assert actual == ["new"]


async def test_changed_tool_implementation_rejects_resume(tmp_path):
    from run_agent_coding.tools import create_coding_tools
    from run_agent_core.tools import AgentToolResult

    opts = options(tmp_path)
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        session_id = app.session.session_id
    original = create_coding_tools

    async def changed(*args, **kwargs):
        return AgentToolResult(content="different implementation")

    def changed_tools(**kwargs):
        tools = list(original(**kwargs))
        tools[0] = replace(tools[0], execute_fn=changed)
        return tuple(tools)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("run_agent_coding.session.create_coding_tools", changed_tools)
        with pytest.raises(ValueError, match="implementation differs"):
            await CodingApplication.open(
                replace(opts, resume=session_id),
                provider=ReplyProvider(),
            )
