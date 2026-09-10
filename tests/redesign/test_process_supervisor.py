import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading

import pytest
from tests.redesign.test_coding_application import ReplyProvider
from tests.redesign.test_gateway_runtime import eventually, released, runtime, submit

from run_agent_coding.host.processes import PosixProcess, ProcessCleanupError, ProcessSupervisor
from run_agent_coding.tools import create_bash_tool
from run_agent_core.messages import AssistantMessage, ToolCall, ToolResultMessage
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.scheduler import GatewayScheduler

__all__ = ["runtime"]


def shell_command(arguments):
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


@pytest.fixture
def commands(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.05)\n", encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "print('child-started', flush=True)\n"
        "if sys.argv[3] == 'wait': time.sleep(60)\n", encoding="utf-8",
    )
    marker = tmp_path / "child.pid"

    def command(mode="wait"):
        return shell_command([sys.executable, str(parent), str(child), str(marker), mode])

    return command, marker


async def child_started(marker):
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.02)
    return int(marker.read_text())


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_process_output_and_exit_code(tmp_path):
    supervisor = ProcessSupervisor()
    result = await supervisor.run(
        shell_command([sys.executable, "-c", "print('hello')"]), cwd=tmp_path,
    )
    assert result.output.strip() == b"hello"
    assert result.exit_code == 0 and not result.cancelled and not result.timed_out
    assert result.events[-2:] == ("empty", "exit:0")
    assert supervisor.active_count == 0
    await supervisor.aclose()


async def test_timeout_terminates_real_descendants(tmp_path, commands):
    command, marker = commands
    supervisor = ProcessSupervisor()
    result = await supervisor.run(command(), cwd=tmp_path, timeout=0.5)
    pid = await child_started(marker)
    assert result.timed_out and not result.cancelled
    assert not alive(pid) and supervisor.active_count == 0
    assert "empty" in result.events
    assert "kill" in result.events or "term" in result.events


async def test_coroutine_cancellation_waits_for_real_descendants(tmp_path, commands):
    command, marker = commands
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run(command(), cwd=tmp_path))
    pid = await child_started(marker)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 8)
    assert not alive(pid) and supervisor.active_count == 0
    await supervisor.aclose()


async def test_successful_root_cannot_leave_detached_child(tmp_path, commands):
    command, marker = commands
    supervisor = ProcessSupervisor()
    result = await supervisor.run(command("exit"), cwd=tmp_path)
    assert result.exit_code == 0 and b"child-started" in result.output
    assert not alive(await child_started(marker))
    assert "empty" in result.events and supervisor.active_count == 0


async def test_close_cancels_inflight_command_and_rejects_new_commands(tmp_path, commands):
    command, marker = commands
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run(command(), cwd=tmp_path))
    pid = await child_started(marker)
    await supervisor.aclose()
    result = await task
    assert result.cancelled and not alive(pid) and supervisor.active_count == 0
    with pytest.raises(RuntimeError, match="closed"):
        await supervisor.run("echo forbidden", cwd=tmp_path)


async def test_cancel_during_spawn_waits_for_ownership_and_cleanup(tmp_path, commands, monkeypatch):
    command, marker = commands
    started, proceed = threading.Event(), threading.Event()
    if os.name == "nt":
        from run_agent_coding.host.windows_jobs import WindowsJobProcess

        process_type = WindowsJobProcess
    else:
        process_type = PosixProcess
    original = process_type.__init__

    def slow_spawn(self, *args, **kwargs):
        original(self, *args, **kwargs)
        started.set()
        if not proceed.wait(5):
            raise TimeoutError("Test did not release spawn")

    monkeypatch.setattr(process_type, "__init__", slow_spawn)
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run(command(), cwd=tmp_path))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pid = await child_started(marker)
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        assert not alive(pid) and supervisor.active_count == 0
    finally:
        proceed.set()
        await supervisor.aclose()


@pytest.mark.skipif(os.name != "posix", reason="POSIX TERM escalation")
async def test_process_ignoring_term_is_force_killed(tmp_path):
    script = tmp_path / "ignore_term.py"
    marker = tmp_path / "ready"
    script.write_text(
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready')\n"
        "while True: time.sleep(0.1)\n", encoding="utf-8",
    )
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run(
        shell_command([sys.executable, str(script), str(marker)]), cwd=tmp_path, timeout=1,
    ))
    result = await task
    assert marker.exists() and result.timed_out
    assert "term" in result.events and "kill" in result.events and "empty" in result.events


async def test_failed_exit_verification_keeps_supervisor_owned(tmp_path, commands, monkeypatch):
    command, marker = commands
    if os.name == "nt":
        from run_agent_coding.host.windows_jobs import WindowsJobProcess

        process_type = WindowsJobProcess
    else:
        process_type = PosixProcess
    active = process_type.active_count
    monkeypatch.setattr(process_type, "active_count", lambda _: 1)
    supervisor = ProcessSupervisor(grace_period=0, cleanup_timeout=0.05)
    try:
        with pytest.raises(ProcessCleanupError):
            await supervisor.run(command("exit"), cwd=tmp_path)
        assert supervisor.active_count == 1
        with pytest.raises(ProcessCleanupError):
            supervisor.assert_empty()
        assert not alive(await child_started(marker))
    finally:
        monkeypatch.setattr(process_type, "active_count", active)
        await supervisor.aclose()
    assert supervisor.active_count == 0


async def test_process_output_limit_is_enforced(tmp_path):
    supervisor = ProcessSupervisor()
    script = tmp_path / "output.py"
    script.write_text("import os\nwhile True: os.write(1, b'x' * 1024)\n", encoding="utf-8")
    command = shell_command([sys.executable, str(script)])
    result = await supervisor.run(command, cwd=tmp_path, timeout=5, output_limit=4096)
    assert result.output_limited and len(result.output) == 4096
    assert supervisor.active_count == 0


async def test_actual_bash_tool_returns_process_exit_evidence(tmp_path, commands):
    command, marker = commands
    tool = create_bash_tool(cwd=tmp_path)
    result = await tool.execute("shell", {"command": command("exit")})
    assert "child-started" in result.text
    assert result.details["exit_code"] == 0
    assert "empty" in result.details["process"]["events"]
    assert not alive(await child_started(marker))


async def test_gateway_stop_exits_real_process_before_releasing_assignment(runtime, tmp_path, commands):
    repo, owner, host = runtime
    command, marker = commands

    class ShellProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(
                reason="toolUse", message=AssistantMessage(
                    content=[ToolCall(id="shell", name="bash", arguments={"command": command()})],
                    model="test", provider="test", stop_reason="toolUse",
                ),
            )

    host.provider_factory = lambda _: ShellProvider()
    runner = CodingAssignmentRunner(host)
    scheduler = GatewayScheduler(repo, owner, runner)
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        pid = await child_started(marker)
        assert alive(pid)
        assert await repo.cancel(owner, receipt.task_id, principal_id="alice") == "cancelling"
        scheduler.signal_cancel(receipt.task_id)
        await eventually(lambda: released(repo, receipt.task_id), timeout=8)
        assert not alive(pid)
        assert not scheduler.errors and not runner.quarantined
        state = await repo.task(receipt.task_id, principal_id="alice")
        assert state["status"] == "cancelled"
        lifecycle = await repo.database.run(lambda c: c.execute(
            "SELECT body_json FROM observations WHERE stream='process.lifecycle' ORDER BY seq"
        ).fetchall())
        records = [json.loads(row[0]) for row in lifecycle]
        assert [r["phase"] for r in records] == ["started", "exited"]
        assert all(r["run_id"] == state["run_id"] for r in records)
        assert "empty" in records[-1]["events"]
    finally:
        await scheduler.shutdown()
        await repo.release_owner(owner)


async def test_gateway_quarantines_when_real_process_exit_cannot_be_verified(
    runtime, tmp_path, commands, monkeypatch,
):
    repo, owner, host = runtime
    command, marker = commands
    if os.name == "nt":
        from run_agent_coding.host.windows_jobs import WindowsJobProcess

        process_type = WindowsJobProcess
    else:
        process_type = PosixProcess
    active = process_type.active_count

    class ShellProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            if not kwargs.get("tools") or any(
                isinstance(message, ToolResultMessage) for message in kwargs["messages"]
            ):
                async for event in super().stream_response(**kwargs):
                    yield event
                return
            yield AssistantDoneEvent(
                reason="toolUse", message=AssistantMessage(
                    content=[ToolCall(id="shell", name="bash", arguments={"command": command("exit")})],
                    model="test", provider="test", stop_reason="toolUse",
                ),
            )

    host.provider_factory = lambda _: ShellProvider()
    original_open = host.open

    async def open_assignment(assignment):
        application = await original_open(assignment)
        application.session._processes.grace_period = 0
        application.session._processes.cleanup_timeout = 0.05
        return application

    monkeypatch.setattr(host, "open", open_assignment)
    monkeypatch.setattr(process_type, "active_count", lambda _: 1)
    runner = CodingAssignmentRunner(host)
    scheduler = GatewayScheduler(repo, owner, runner)
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        await child_started(marker)

        async def contained():
            state = await repo.task(receipt.task_id, principal_id="alice")
            return state if state["workspace_status"] == "quarantined" else None

        state = await eventually(contained, timeout=8)
        assert state["released"] == 0 and state["status"] == "outcome_unknown"
        assert runner.quarantined and scheduler.errors
        await repo.admit(owner, submit(tmp_path, "next"), model="test")
        assert await repo.claim_next(owner) is None
    finally:
        monkeypatch.setattr(process_type, "active_count", active)
        for application in runner.quarantined.values():
            await application.session._processes.aclose()
        await scheduler.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object host death contract")
async def test_windows_host_death_closes_job_and_kills_descendants(tmp_path, commands):
    command, marker = commands
    launcher = tmp_path / "host.py"
    launcher.write_text(
        "import asyncio, pathlib, sys\n"
        "from run_agent_coding.host.processes import ProcessSupervisor\n"
        "asyncio.run(ProcessSupervisor().run(sys.argv[1], cwd=pathlib.Path(sys.argv[2])))\n",
        encoding="utf-8",
    )
    with open(tmp_path / "host.log", "wb") as output:
        host = subprocess.Popen(
            [sys.executable, str(launcher), command(), str(tmp_path)],
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            pid = await child_started(marker)
            assert alive(pid)
            host.kill()
            await asyncio.to_thread(host.wait, timeout=5)
            async with asyncio.timeout(5):
                while alive(pid):
                    await asyncio.sleep(0.02)
        finally:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)
