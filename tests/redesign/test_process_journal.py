import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest
from tests.redesign.test_process_supervisor import alive, child_started, commands

from run_agent_coding.host.process_identity import current_process_identity, process_identity
from run_agent_coding.host.processes import ProcessSupervisor
from run_agent_coding.storage.processes import SqliteProcessJournal
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.session.contracts import RunOutcome, SessionConflict, StaleRunToken

__all__ = ["commands"]


async def test_process_journal_fences_identity_and_blocks_unresolved_completion(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sessions = SqliteSessionRepository(database)
        await sessions.create_session(
            cwd=tmp_path,
            model="test",
            session_id="session",
            principal_id="local",
        )
        token = await sessions.claim("session", owner_id="host", run_id="initial")
        token = await sessions.begin_run(token, branch_id="main", run_id="run")
        journal = SqliteProcessJournal(database)
        await journal.record(token, {"process_id": "p", "phase": "launching"})
        outcome = RunOutcome(token, "main", "succeeded", None, ())
        with pytest.raises(SessionConflict, match="unresolved process"):
            await sessions.complete_run(outcome)
        with pytest.raises(SessionConflict, match="writer"):
            await journal.record(
                replace(token, owner_id="other"),
                {
                    "process_id": "p",
                    "phase": "started",
                },
            )
        with pytest.raises(ValueError, match="verified empty"):
            await journal.record(token, {"process_id": "p", "phase": "exited"})
        await journal.record(token, {"process_id": "p", "phase": "started", "pid": 123})
        await journal.record(token, {"process_id": "p", "phase": "exited", "events": ["empty"]})
        assert (await sessions.complete_run(outcome)).status == "succeeded"
        with pytest.raises(StaleRunToken):
            await journal.record(token, {"process_id": "late", "phase": "launching"})


def test_host_identity_is_stable_and_dead_pid_is_not_current():
    assert current_process_identity() == process_identity(os.getpid())
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    child.wait(timeout=5)
    assert process_identity(child.pid) is None


async def test_failed_start_record_never_executes_user_command(tmp_path, commands):
    command, marker = commands
    supervisor = ProcessSupervisor()
    phases = []

    async def record(payload):
        phases.append(payload["phase"])
        if payload["phase"] == "started":
            raise RuntimeError("Injected durable record failure")

    supervisor.recorder_factory = lambda: record
    with pytest.raises(RuntimeError, match="durable record"):
        await supervisor.run(command(), cwd=tmp_path)
    assert phases == ["launching", "started", "exited"]
    assert supervisor.active_count == 0 and not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows job close after abrupt host death")
async def test_killed_coding_host_leaves_durable_identity_but_no_live_child(tmp_path, commands):
    command, marker = commands
    worker = tmp_path / "coding_host.py"
    worker.write_text(
        "import asyncio, pathlib, sys\n"
        "from run_agent_coding.application import CodingApplication, ApplicationOptions\n"
        "from run_agent_coding.paths import RunAgentPaths\n"
        "from run_agent_core.messages import AssistantMessage, ToolCall\n"
        "from run_agent_core.provider_events import AssistantDoneEvent\n"
        "class Provider:\n"
        "    async def stream_response(self, **kwargs):\n"
        "        tools = bool(kwargs.get('tools'))\n"
        "        yield AssistantDoneEvent(reason='toolUse' if tools else 'stop',\n"
        "            message=AssistantMessage(model='test', provider='test',\n"
        "                stop_reason='toolUse' if tools else 'stop',\n"
        "                content=[ToolCall(id='shell',name='bash',arguments={'command':sys.argv[1]})]\n"
        "                if tools else 'name'))\n"
        "async def main():\n"
        "    root=pathlib.Path(sys.argv[2])\n"
        "    options=ApplicationOptions(cwd=root, model='test', provider_name='test',\n"
        "        paths=RunAgentPaths(home=root/'state',agents_home=root/'agents'),\n"
        "        extensions_enabled=False)\n"
        "    async with await CodingApplication.open(options,provider=Provider()) as app:\n"
        "        async for event in app.prompt('run command'): pass\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    with (tmp_path / "coding.log").open("wb") as log:
        host = subprocess.Popen(
            [sys.executable, str(worker), command(), str(tmp_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            pid = await child_started(marker)
            database_path = tmp_path / "state" / "state.sqlite3"
            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT intent_json,native_json,status FROM managed_processes"
                ).fetchone()
            intent, native = json.loads(row[0]), json.loads(row[1])
            assert row[2] == "running"
            assert intent["host_identity"] == process_identity(intent["host_pid"])
            assert native["native_identity"] == process_identity(native["pid"])
            host.kill()
            await asyncio.to_thread(host.wait, timeout=5)
            async with asyncio.timeout(5):
                while alive(pid):
                    await asyncio.sleep(0.02)
            assert process_identity(host.pid) is None
            assert process_identity(intent["host_pid"]) is None
            assert process_identity(native["pid"]) is None
            with sqlite3.connect(database_path) as connection:
                assert (
                    connection.execute("SELECT status FROM managed_processes").fetchone()[0]
                    == "running"
                )
                assert (
                    connection.execute("SELECT status FROM executions").fetchone()[0] == "running"
                )
        finally:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)
