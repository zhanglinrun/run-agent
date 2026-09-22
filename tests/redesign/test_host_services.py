import asyncio
import dataclasses
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionError, SessionShutdownContext
from run_agent_coding.host.contracts import HeadChange, StateChange
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.storage.host import ExtensionRetired


@pytest.fixture
def extension(tmp_path):
    path = tmp_path / "extension.py"
    path.write_text(
        """
def setup(api):
    async def remember(args, context):
        from run_agent_coding.host.contracts import StateChange
        state = context.api.context.services.scope("project").state
        previous = await state.get("note")
        if args:
            await state.compare_and_set(StateChange("note", previous.version if previous else 0, args))
        return (await state.get("note")).value
    api.register_command("remember", remember, description="Save project note")
""",
        encoding="utf-8",
    )
    return path


def context(app):
    runtime = app.session.extension_runtime
    return runtime._fresh_context(runtime._extensions[0].source_id)


async def test_services_are_unavailable_during_setup_and_state_survives_reload(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        with pytest.raises(ExtensionError, match="after session activation"):
            _ = context(app).services
        await app.start()
        old = context(app).services.scope("project")
        result = await app.command("/remember sqlite note")
        assert result.message == "sqlite note"
        artifact = await old.artifacts.put(b"skill helper")
        version = await old.resources.put_immutable("skill", "v1", artifacts=[artifact])
        await old.resources.advance_head(HeadChange("skill", None, version.version, "test", {}))
        await app.command("/reload")
        for work in (
            old.state.get("note"),
            old.state.compare_and_set(StateChange("late", 0, "rejected")),
            old.resources.snapshot(),
            old.artifacts.read(artifact),
        ):
            with pytest.raises((ExtensionError, ExtensionRetired)):
                await work
        fresh = context(app).services.scope("project")
        assert (await fresh.state.get("note")).value == "sqlite note"
        assert await fresh.artifacts.read(artifact) == b"skill helper"
        assert (await fresh.resources.resolve("skill", version.version)).content == "v1"


async def test_scopes_bind_principal_project_session_and_source(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    first = await CodingApplication.open(opts, provider=ReplyProvider())
    await first.start()
    state = context(first).services
    await state.scope("session").state.compare_and_set(StateChange("note", 0, "session-only"))
    await state.scope("project").state.compare_and_set(StateChange("note", 0, "project-only"))
    await state.scope("user").state.compare_and_set(StateChange("note", 0, "user-wide"))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as second:
        await second.start()
        other = context(second).services
        assert await other.scope("session").state.get("note") is None
        assert (await other.scope("project").state.get("note")).value == "project-only"
    project = tmp_path / "other-project"
    project.mkdir()
    async with await CodingApplication.open(
        replace(opts, cwd=project), provider=ReplyProvider()
    ) as third:
        await third.start()
        other = context(third).services
        assert await other.scope("project").state.get("note") is None
        assert (await other.scope("user").state.get("note")).value == "user-wide"
        with pytest.raises(ValueError, match="Scope"):
            other.scope("another-user")
    manager = SessionManager(opts.paths, principal_id="someone-else")
    try:
        async with await CodingApplication.open(
            opts, provider=ReplyProvider(), manager=manager
        ) as app:
            await app.start()
            assert await context(app).services.scope("user").state.get("note") is None
    finally:
        await manager.aclose()
        await first.aclose()


async def test_artifact_hash_does_not_bypass_extension_scope(tmp_path, extension):
    second_source = tmp_path / "another.py"
    second_source.write_text("def setup(api): pass", encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension, second_source))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        runtime = app.session.extension_runtime
        first, second = [
            runtime._fresh_context(item.source_id).services.scope("project")
            for item in runtime._extensions
        ]
        ref = await first.artifacts.put(b"private")
        with pytest.raises(KeyError, match="scope"):
            await second.artifacts.read(ref)
        with pytest.raises(KeyError, match="scope"):
            await second.resources.put_immutable("foreign", "not owned", artifacts=[ref])


async def test_failed_host_publication_keeps_live_runtime_and_namespace(
    tmp_path, extension, monkeypatch
):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        old_runtime = app.session.extension_runtime
        await app.command("/remember still live")

        async def fail(*args, **kwargs):
            raise OSError("binding commit rejected")

        monkeypatch.setattr(app.session.host_services, "publish", fail)
        with pytest.raises(OSError, match="binding commit"):
            await app.command("/reload")
        assert app.session.extension_runtime is old_runtime
        assert old_runtime.active
        assert (await app.command("/remember")).message == "still live"


async def test_failed_publish_does_not_shutdown_or_dispose_live_runtime(tmp_path, monkeypatch):
    shutdown_marker = tmp_path / "shutdown.txt"
    dispose_marker = tmp_path / "dispose.txt"
    source = tmp_path / "lifecycle.py"
    source.write_text(
        f"""from pathlib import Path

def setup(api):
    generation = api.context.generation_id

    async def ping(args, context):
        return "live"

    async def shutdown(event, context):
        Path({str(shutdown_marker)!r}).write_text(event.reason, encoding="utf-8")

    async def dispose():
        Path({str(dispose_marker)!r}).write_text(generation, encoding="utf-8")

    api.register_command("ping", ping)
    api.on("session_shutdown", shutdown)
    api.register_disposer(dispose)
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(source,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        old_generation = app.session.extension_runtime._generation.id

        async def fail(*args, **kwargs):
            raise OSError("publish failed")

        monkeypatch.setattr(app.session.host_services, "publish", fail)
        with pytest.raises(OSError, match="publish failed"):
            await app.command("/reload")

        assert not shutdown_marker.exists()
        assert dispose_marker.read_text(encoding="utf-8") != old_generation
        assert (await app.command("/ping")).message == "live"


async def test_cancel_at_binding_commit_finishes_one_consistent_reload(
    tmp_path, extension, monkeypatch
):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        old_runtime = app.session.extension_runtime
        host = app.session.host_services
        original = host.publish
        committed, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            services = await original(*args, **kwargs)
            committed.set()
            await release.wait()
            return services

        monkeypatch.setattr(host, "publish", delayed)
        operation = asyncio.create_task(app.command("/reload"))
        await committed.wait()
        operation.cancel()
        release.set()
        await operation
        assert not old_runtime.active
        assert app.session.extension_runtime.active
        assert (await app.command("/remember fresh")).message == "fresh"


async def test_new_session_retirement_does_not_rebind_captured_scope(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        old_id = app.session.session_id
        old = context(app).services.scope()
        await old.state.compare_and_set(StateChange("note", 0, "old"))
        await app.command("/new")
        assert app.session.session_id != old_id
        with pytest.raises((ExtensionError, ExtensionRetired)):
            await old.state.compare_and_set(StateChange("late", 0, "old"))
        assert await context(app).services.scope().state.get("note") is None


def test_shutdown_context_is_frozen_and_only_exposes_reason_session_and_cwd():
    assert [field.name for field in dataclasses.fields(SessionShutdownContext)] == [
        "reason",
        "session_id",
        "cwd",
    ]
    context = SessionShutdownContext(reason="reload", session_id="s-1", cwd=Path("."))
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.reason = "quit"  # type: ignore[misc]
    for unavailable in ("services", "telemetry", "ui", "generation_id", "is_active"):
        assert not hasattr(context, unavailable)


async def test_shutdown_notification_is_single_and_receives_the_minimal_context(tmp_path):
    shutdown_log = tmp_path / "shutdown.log"
    source = tmp_path / "shutdown_context.py"
    source.write_text(
        f"""\
from pathlib import Path


def setup(api):
    log = Path({str(shutdown_log)!r})

    async def shutdown(event, context):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{{event.reason}}|{{context.reason}}|{{context.session_id}}"
                f"|{{context.cwd.name}}|{{type(context).__name__}}"
                f"|{{hasattr(context, 'services')}}\\n"
            )

    api.on("session_shutdown", shutdown)
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(source,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        session_id = app.session.session_id
        events = [event async for event in app.prompt("first")]
        head = events[-1].head_id
        recorded = f"{session_id}|{tmp_path.name}|SessionShutdownContext|False"

        await app.command("/reload")
        assert shutdown_log.read_text(encoding="utf-8").splitlines() == [
            f"reload|reload|{recorded}"
        ]
        # A branch reuses the live runtime, so it must not notify shutdown.
        await app.command(f"/branch {head}")
        assert shutdown_log.read_text(encoding="utf-8").splitlines() == [
            f"reload|reload|{recorded}"
        ]
    assert shutdown_log.read_text(encoding="utf-8").splitlines() == [
        f"reload|reload|{recorded}",
        f"quit|quit|{recorded}",
    ]
