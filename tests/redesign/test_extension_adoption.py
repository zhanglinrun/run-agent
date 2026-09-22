from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.adoption import finish_committed_adoption
from run_agent_coding.extensions.api import ExtensionError, ExtensionGeneration
from run_agent_coding.extensions.runtime import RuntimeCloseResult


def test_generation_retiring_is_read_only_then_stale() -> None:
    parent = ExtensionGeneration()
    child = ExtensionGeneration(parent=parent)

    parent.begin_retiring()
    assert parent.state == "retiring"
    assert child.state == "retiring"
    child.assert_readable()
    with pytest.raises(ExtensionError):
        child.assert_active()

    parent.invalidate()
    assert child.state == "retired"
    with pytest.raises(ExtensionError):
        child.assert_readable()


@pytest.mark.anyio
async def test_committed_adoption_orders_notification_before_cleanup() -> None:
    events: list[str] = []

    class Runtime:
        def begin_retiring(self) -> None:
            events.append("retiring")

        async def emit_session_shutdown(self, reason: str) -> None:
            events.append(f"shutdown:{reason}")

        def clear_ui_status(self) -> None:
            events.append("clear-ui")

        async def aclose(self) -> RuntimeCloseResult:
            events.append("close")
            return RuntimeCloseResult(drained=True)

    result = await finish_committed_adoption(Runtime(), "reload")  # type: ignore[arg-type]
    assert result.notice is None
    assert result.cancelled is False
    assert events == ["retiring", "shutdown:reload", "clear-ui", "close"]


@pytest.mark.anyio
async def test_committed_cleanup_contains_caller_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Runtime:
        def begin_retiring(self) -> None:
            pass

        async def emit_session_shutdown(self, reason: str) -> None:
            del reason

        def clear_ui_status(self) -> None:
            pass

        async def aclose(self) -> RuntimeCloseResult:
            entered.set()
            await release.wait()
            return RuntimeCloseResult(drained=True)

    adoption = asyncio.create_task(
        finish_committed_adoption(Runtime(), "reload")  # type: ignore[arg-type]
    )
    await entered.wait()
    adoption.cancel()
    await asyncio.sleep(0)
    assert not adoption.done()
    release.set()
    result = await adoption
    assert result.cancelled is True


def lifecycle_source(*, shutdown_log: Path, dispose_log: Path) -> str:
    return f"""\
from pathlib import Path

from run_agent_coding.extensions.api import ExtensionError
from run_agent_core.tools import AgentTool, AgentToolResult


def setup(api):
    shutdown_log = Path({str(shutdown_log)!r})
    dispose_log = Path({str(dispose_log)!r})
    generation = api.context.generation_id
    api.context.ui.set_status("probe", "live")

    async def echo(*args, **kwargs):
        return AgentToolResult(content=[])

    api.register_tool(
        AgentTool(
            name="echo",
            label="Echo",
            description="Echoes nothing.",
            parameters={{"type": "object"}},
            execute_fn=echo,
            execution_mode="sequential",
        )
    )

    async def ping(args, context):
        api.context.ui.set_status("probe", "live")
        return "live"

    async def late():
        return None

    async def shutdown(event, context):
        rejected = False
        try:
            api.register_disposer(late)
        except ExtensionError:
            rejected = True
        with shutdown_log.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{{event.reason}}|{{context.reason}}|{{context.session_id}}|{{rejected}}\\n"
            )

    async def dispose():
        with dispose_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{{generation}}\\n")

    api.register_command("ping", ping)
    api.on("session_shutdown", shutdown)
    api.register_disposer(dispose)
"""


class RecordingUiBridge:
    """Minimal UiBridge that records status writes and clears."""

    def __init__(self) -> None:
        self.has_ui = True
        self.status: dict[str, str | None] = {}
        self.clear_calls = 0

    def notify(self, message: str, level: str = "info") -> None:
        del message, level

    async def select(self, title: str, options: object, *, timeout=None) -> None:
        del title, options, timeout
        return None

    async def confirm(self, title: str, message: str, *, timeout=None) -> bool:
        del title, message, timeout
        return False

    async def input(self, title: str, placeholder: str = "", *, secret=False, timeout=None) -> None:
        del title, placeholder, secret, timeout
        return None

    def set_status(self, source: str, key: str, text: str | None) -> None:
        del source
        self.status[key] = text

    def clear_status(self, source: str | None = None) -> None:
        del source
        self.clear_calls += 1
        self.status.clear()


async def open_lifecycle_app(tmp_path):
    shutdown_log = tmp_path / "shutdown.log"
    dispose_log = tmp_path / "dispose.log"
    source = tmp_path / "lifecycle.py"
    source.write_text(
        lifecycle_source(shutdown_log=shutdown_log, dispose_log=dispose_log),
        encoding="utf-8",
    )
    app = await CodingApplication.open(
        replace(options(tmp_path), extension_paths=(source,)), provider=ReplyProvider()
    )
    ui = RecordingUiBridge()
    await app.start(ui)
    return app, ui, shutdown_log, dispose_log


def assert_branch_failure_kept_live_state(app, ui, shutdown_log, dispose_log, runtime, generation):
    assert app.session.extension_runtime is runtime
    assert runtime.active
    assert runtime._generation.id == generation
    assert [tool.name for tool in runtime.extension_tools] == ["echo"]
    assert ui.status == {"probe": "live"}
    assert ui.clear_calls == 0
    assert not shutdown_log.exists()
    assert not dispose_log.exists()


async def test_branch_activation_failure_keeps_runtime_tools_ui_and_generation(
    tmp_path, monkeypatch
):
    app, ui, shutdown_log, dispose_log = await open_lifecycle_app(tmp_path)
    try:
        events = [event async for event in app.prompt("first")]
        head = events[-1].head_id
        runtime = app.session.extension_runtime
        generation = runtime._generation.id
        assert (await app.command("/ping")).message == "live"

        async def fail(*args, **kwargs):
            raise OSError("branch activation failed")

        monkeypatch.setattr(app.session, "_prepare_resource_activation", fail)
        with pytest.raises(OSError, match="branch activation failed"):
            await app.session.branch_to_entry(head)

        assert_branch_failure_kept_live_state(
            app, ui, shutdown_log, dispose_log, runtime, generation
        )
        assert (await app.command("/ping")).message == "live"
    finally:
        await app.aclose()


async def test_branch_publish_failure_keeps_runtime_tools_ui_and_generation(tmp_path, monkeypatch):
    app, ui, shutdown_log, dispose_log = await open_lifecycle_app(tmp_path)
    try:
        events = [event async for event in app.prompt("first")]
        head = events[-1].head_id
        runtime = app.session.extension_runtime
        generation = runtime._generation.id
        old_leaf = app.session._last_parent_id
        old_snapshot = app.session._resource_snapshot_id
        assert (await app.command("/ping")).message == "live"

        async def fail(*args, **kwargs):
            raise OSError("branch publication failed")

        monkeypatch.setattr(app.session.storage, "fork", fail)
        with pytest.raises(OSError, match="branch publication failed"):
            await app.session.branch_to_entry(head)

        assert_branch_failure_kept_live_state(
            app, ui, shutdown_log, dispose_log, runtime, generation
        )
        assert app.session._last_parent_id == old_leaf
        assert app.session._resource_snapshot_id == old_snapshot
        assert (await app.command("/ping")).message == "live"
    finally:
        await app.aclose()


async def test_branch_reuse_keeps_generation_and_never_shuts_down_live_runtime(tmp_path):
    app, ui, shutdown_log, dispose_log = await open_lifecycle_app(tmp_path)
    session_id = app.session.session_id
    events = [event async for event in app.prompt("first")]
    head = events[-1].head_id
    runtime = app.session.extension_runtime
    generation = runtime._generation.id
    assert (await app.command("/ping")).message == "live"

    result = await app.session.branch_to_entry(head)

    assert "Branched session" in result.message
    assert_branch_failure_kept_live_state(app, ui, shutdown_log, dispose_log, runtime, generation)
    assert (await app.command("/ping")).message == "live"
    await app.aclose()
    # Quit is the only shutdown notification; the reused generation disposes
    # once, and its retiring state already rejected handler registration.
    assert shutdown_log.read_text(encoding="utf-8").splitlines() == [f"quit|quit|{session_id}|True"]
    assert dispose_log.read_text(encoding="utf-8").splitlines() == [generation]
