import asyncio
import json
import os
import subprocess
import sys

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_gateway_runtime import runtime, submit
from tests.redesign.test_process_supervisor import alive, child_started, commands

from run_agent_coding.host.process_probe import inspect_native_process
from run_agent_coding.storage.backup import create_backup, restore_backup
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.session.contracts import SessionConflict
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.contracts import GatewayOwnershipLost
from run_agent_gateway.ownership import GatewayProcessLock
from run_agent_gateway.recovery import GatewayRecovery
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.runtime import GatewayCodingRuntime

__all__ = ["runtime", "commands"]

WORKER = """
import asyncio,json,os,pathlib,sys
from run_agent_coding.application import ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import AssistantMessage,ToolCall
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_gateway.contracts import RouteIdentity,Submission
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.ownership import GatewayProcessLock
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.coding import CodingAssignmentRunner
class Provider:
    async def stream_response(self, **kwargs):
        tool = sys.argv[2] == 'running' and bool(kwargs.get('tools'))
        yield AssistantDoneEvent(reason='toolUse' if tool else 'stop',
            message=AssistantMessage(model='test',provider='test',
                stop_reason='toolUse' if tool else 'stop',
                content=[ToolCall(id='shell',name='bash',arguments={'command':sys.argv[3]})]
                if tool else 'done'))
async def main():
    root=pathlib.Path(sys.argv[1])
    paths=RunAgentPaths(home=root/'state',agents_home=root/'agents')
    lock=GatewayProcessLock(paths.home/'gateway.lock'); lock.acquire()
    db=await SqliteDatabase.open(paths.database_path)
    repo=GatewayRepository(db); await repo.initialize()
    owner=await repo.acquire_owner('dead-worker', process_lock=lock, lease_seconds=120)
    value=Submission(RouteIdentity('local','account','chat',subject_id='alice'),
        'alice','first','first task',root)
    first=await repo.admit(owner,value,model='test')
    from dataclasses import replace
    second=await repo.admit(owner,replace(value,source_message_id='second',content='second task'),model='test')
    assignment=await repo.claim_next(owner)
    (root/'ready.json').write_text(json.dumps({'first':first.task_id,'second':second.task_id,
        'run_id':assignment.run_id,'session_id':first.session_id}))
    if sys.argv[2] != 'claim':
        runtime=GatewayCodingRuntime(repo,owner,ApplicationOptions(cwd=root,paths=paths,
            model='test',provider_name='test',extensions_enabled=False),
            provider_factory=lambda _: Provider())
        await CodingAssignmentRunner(runtime).run(assignment,asyncio.Event())
    os._exit(77)
asyncio.run(main())
"""


@pytest.fixture
def crash_worker(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")

    def launch(mode="claim", command=""):
        return subprocess.Popen(
            [sys.executable, str(worker), str(tmp_path), mode, command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    return launch


async def crashed(worker):
    output, error = await asyncio.to_thread(worker.communicate, timeout=15)
    assert worker.returncode == 77, (output, error)


async def test_crash_before_prompt_requires_review_then_next_task_uses_same_history(tmp_path, crash_worker):
    await crashed(crash_worker())
    info = json.loads((tmp_path / "ready.json").read_text())
    lock = GatewayProcessLock(tmp_path / "state" / "gateway.lock")
    lock.acquire()
    try:
        async with await SqliteDatabase.open(tmp_path / "state" / "state.sqlite3") as database:
            repo = GatewayRepository(database)
            await repo.initialize()
            owner = await repo.acquire_owner("recovery", process_lock=lock)
            recovery = GatewayRecovery(repo, owner)
            reports = await recovery.reconcile()
            assert len(reports) == 1 and reports[0]["processes_empty"]
            assert reports[0]["review_required"] and not reports[0]["released"]
            assert await repo.claim_next(owner) is None
            with pytest.raises(SessionConflict, match="recovery"):
                await repo.sessions.claim(
                    info["session_id"], owner_id="cli-bypass", run_id="bypass", takeover=True,
                )
            result = await recovery.release(info["run_id"], note="Inspected workspace; no changes")
            assert result["released"]
            assert (await repo.task(info["first"], principal_id="alice"))["status"] == "outcome_unknown"
            assignment = await repo.claim_next(owner)
            assert assignment.task_id == info["second"] and assignment.session_id == info["session_id"]
            host = GatewayCodingRuntime(repo, owner, options(tmp_path), provider_factory=lambda _: ReplyProvider())
            await CodingAssignmentRunner(host).run(assignment, asyncio.Event())
            await repo.release(owner, assignment)
            assert (await repo.task(info["second"], principal_id="alice"))["status"] == "succeeded"
            await repo.release_owner(owner)
    finally:
        lock.close()


async def test_committed_result_auto_releases_after_dead_host_without_duplicate_delivery(tmp_path, crash_worker):
    await crashed(crash_worker("complete"))
    info = json.loads((tmp_path / "ready.json").read_text())
    lock = GatewayProcessLock(tmp_path / "state" / "gateway.lock")
    lock.acquire()
    try:
        async with await SqliteDatabase.open(tmp_path / "state" / "state.sqlite3") as database:
            repo = GatewayRepository(database)
            await repo.initialize()
            before = await database.run(lambda c: dict(c.execute(
                "SELECT * FROM gateway_outbox WHERE task_id=? AND kind='result'", (info["first"],)
            ).fetchone()))
            owner = await repo.acquire_owner("replacement", process_lock=lock)
            await GatewayRecovery(repo, owner).reconcile()
            state = await repo.task(info["first"], principal_id="alice")
            assert state["released"] and state["status"] == "succeeded"
            after = await database.run(lambda c: dict(c.execute(
                "SELECT * FROM gateway_outbox WHERE task_id=? AND kind='result'", (info["first"],)
            ).fetchone()))
            assert before == after
            await repo.release_owner(owner)
    finally:
        lock.close()


async def test_live_owner_blocks_recovery_and_foreign_machine_is_not_verified(runtime, tmp_path):
    repo, owner, _ = runtime
    await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    await repo.contain(owner, assignment, error="test")
    recovery = GatewayRecovery(repo, owner)
    assert not (await recovery.inspect(assignment.run_id))["previous_host_exited"]
    with pytest.raises(SessionConflict, match="verified stopped"):
        await recovery.release(assignment.run_id, note="cannot override live process")
    await repo.database.run(lambda c: c.execute(
        "UPDATE gateway_hosts SET process_json=?",
        ('{"pid":99999999,"identity":"other","machine_identity":"foreign"}',),
    ), write=True)
    assert not (await recovery.inspect(assignment.run_id))["processes_empty"]


async def test_shell_crash_recovery_checks_native_job_and_preserves_unknown_effect(tmp_path, crash_worker, commands):
    command, marker = commands
    worker = crash_worker("running", command())
    try:
        pid = await child_started(marker)
        worker.kill()
        await asyncio.to_thread(worker.communicate, timeout=5)
        if os.name == "nt":
            async with asyncio.timeout(5):
                while alive(pid):
                    await asyncio.sleep(0.02)
        info = json.loads((tmp_path / "ready.json").read_text())
        lock = GatewayProcessLock(tmp_path / "state" / "gateway.lock")
        lock.acquire()
        try:
            async with await SqliteDatabase.open(tmp_path / "state" / "state.sqlite3") as database:
                repo = GatewayRepository(database)
                await repo.initialize()
                owner = await repo.acquire_owner("replace-shell", process_lock=lock)
                recovery = GatewayRecovery(repo, owner)
                report = await recovery.inspect(info["run_id"])
                if os.name == "posix":
                    assert not report["processes_empty"]
                    with pytest.raises(SessionConflict, match="verified stopped"):
                        await recovery.release(info["run_id"], note="process still live")
                    report = await recovery.terminate(info["run_id"])
                assert report["processes_empty"] and len(report["processes"]) == 1
                await recovery.release(info["run_id"], note="Reviewed command artifacts")
                state = await repo.task(info["first"], principal_id="alice")
                assert state["status"] == "outcome_unknown" and state["released"]
                assert await database.run(lambda c: c.execute(
                    "SELECT status FROM managed_processes"
                ).fetchone()[0]) == "exited"
                await repo.release_owner(owner)
        finally:
            lock.close()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)


async def test_restored_database_does_not_restart_gateway_or_repeat_delivery(runtime, tmp_path):
    repo, owner, _ = runtime
    await repo.admit(owner, submit(tmp_path), model="test")
    backup = await create_backup(repo.database, repo.workspaces.artifacts, tmp_path / "backup")
    restored = await restore_backup(backup, tmp_path / "restored")
    async with await SqliteDatabase.open(restored / "state.sqlite3") as database:
        repository = GatewayRepository(database)
        await repository.initialize()
        with pytest.raises(GatewayOwnershipLost, match="Restored state"):
            await repository.acquire_owner("restored")


async def test_recovery_commit_rollback_retains_slot_workspace_and_notification(tmp_path, crash_worker):
    await crashed(crash_worker())
    info = json.loads((tmp_path / "ready.json").read_text())
    lock = GatewayProcessLock(tmp_path / "state" / "gateway.lock")
    lock.acquire()
    try:
        async with await SqliteDatabase.open(tmp_path / "state" / "state.sqlite3") as database:
            repo = GatewayRepository(database)
            await repo.initialize()
            owner = await repo.acquire_owner("rollback", process_lock=lock)
            def fail(point):
                if point == "gateway_recovery_committed":
                    raise RuntimeError("Injected recovery rollback")
            repo.fault = fail
            recovery = GatewayRecovery(repo, owner)
            with pytest.raises(RuntimeError, match="rollback"):
                await recovery.release(info["run_id"], note="reviewed")
            task = await repo.task(info["first"], principal_id="alice")
            assert not task["released"] and task["workspace_status"] == "quarantined"
            assert await database.run(lambda c: c.execute(
                "SELECT COUNT(*) FROM gateway_outbox WHERE task_id=? AND kind='result'",
                (info["first"],),
            ).fetchone()[0]) == 0
            repo.fault = None
            first = await recovery.release(info["run_id"], note="reviewed")
            second = await recovery.release(info["run_id"], note="same review")
            assert first["released"] and second["released"]
            assert await database.run(lambda c: c.execute(
                "SELECT COUNT(*) FROM gateway_outbox WHERE task_id=? AND kind='result'",
                (info["first"],),
            ).fetchone()[0]) == 1
            await repo.release_owner(owner)
    finally:
        lock.close()


async def test_recovery_command_inspects_and_releases_without_loading_channels(tmp_path, crash_worker):
    await crashed(crash_worker())
    info = json.loads((tmp_path / "ready.json").read_text())
    def invoke(*arguments):
        return subprocess.run(
            [sys.executable, "-m", "run_agent_gateway.cli", "recover", "--state-dir",
             str(tmp_path / "state"), *arguments], capture_output=True, text=True, timeout=15,
        )
    inspected = await asyncio.to_thread(invoke)
    assert inspected.returncode == 0, inspected.stderr
    report = json.loads(inspected.stdout)[0]
    assert report["run_id"] == info["run_id"] and not report["released"]
    refused = await asyncio.to_thread(invoke, "--release", info["run_id"])
    assert refused.returncode != 0 and "--note" in refused.stderr
    released = await asyncio.to_thread(invoke, "--release", info["run_id"], "--note", "Reviewed outputs")
    assert released.returncode == 0, released.stderr
    assert json.loads(released.stdout)[0]["released"]
    assert json.loads((await asyncio.to_thread(invoke)).stdout) == []


async def test_host_death_before_native_record_never_releases_user_command(tmp_path, commands):
    command, marker = commands
    worker = tmp_path / "spawn_crash.py"
    worker.write_text(
        "import asyncio,json,os,pathlib,sys\n"
        "from run_agent_coding.host.processes import ProcessSupervisor\n"
        "async def record(payload):\n"
        "    if payload['phase']=='launching':\n"
        "        pathlib.Path(sys.argv[2]).write_text(json.dumps(payload))\n"
        "    if payload['phase']=='started': os._exit(77)\n"
        "async def main():\n"
        "    supervisor=ProcessSupervisor(); supervisor.recorder_factory=lambda:record\n"
        "    await supervisor.run(sys.argv[1],cwd=pathlib.Path.cwd())\n"
        "asyncio.run(main())\n", encoding="utf-8",
    )
    intent_path = tmp_path / "intent.json"
    child = subprocess.Popen(
        [sys.executable, str(worker), command(), str(intent_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    await crashed(child)
    intent = json.loads(intent_path.read_text())
    async with asyncio.timeout(5):
        while not inspect_native_process(intent, None)["empty"]:
            await asyncio.sleep(0.02)
    assert not marker.exists()
