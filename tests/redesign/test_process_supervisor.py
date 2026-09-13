import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass

import pytest

from run_agent_coding.host.process_identity import process_identity
from run_agent_coding.host.processes import PosixProcess, ProcessCleanupError, ProcessSupervisor
from run_agent_coding.tools import create_bash_tool


def shell_command(arguments):
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


@pytest.fixture
def commands(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "import json, os, pathlib, sys, time\n"
        "from run_agent_coding.host.process_identity import process_identity\n"
        "pid = os.getpid()\n"
        "pathlib.Path(sys.argv[1]).write_text(\n"
        "    json.dumps({'pid': pid, 'identity': process_identity(pid)}))\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "print('child-started', flush=True)\n"
        "if sys.argv[3] == 'wait': time.sleep(60)\n",
        encoding="utf-8",
    )
    marker = tmp_path / "child.pid"

    def command(mode="wait"):
        return shell_command([sys.executable, str(parent), str(child), str(marker), mode])

    return command, marker


@dataclass(frozen=True)
class Child:
    """A started descendant, identified by PID plus its native process identity."""

    pid: int
    identity: str


async def child_started(marker):
    async with asyncio.timeout(5):
        while True:
            if marker.exists():
                try:
                    record = json.loads(marker.read_text())
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("identity"):
                    return Child(record["pid"], record["identity"])
            await asyncio.sleep(0.02)


def alive(child):
    """An exited or PID-reused process is not the child that was started."""
    return child.pid > 0 and process_identity(child.pid) == child.identity


async def test_process_output_and_exit_code(tmp_path):
    supervisor = ProcessSupervisor()
    result = await supervisor.run(
        shell_command([sys.executable, "-c", "print('hello')"]),
        cwd=tmp_path,
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
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        assert supervisor.active_count == 0 and not marker.exists()
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
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(
        supervisor.run(
            shell_command([sys.executable, str(script), str(marker)]),
            cwd=tmp_path,
            timeout=1,
        )
    )
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
            stdout=output,
            stderr=subprocess.STDOUT,
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
