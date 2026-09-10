"""Verify a clean wheel installation outside the repository and without a model."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SMOKE = """
import asyncio, json, os, pathlib, shlex, subprocess, sys
from importlib.metadata import distribution
import run_agent_entry
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.telemetry import SqliteTelemetrySink
from run_agent_core.session.entries import MessageEntry
from run_agent_core.messages import UserMessage
from run_agent_coding.application import CodingApplication, ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.host.contracts import StateChange, HeadChange
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_gateway.contracts import RouteIdentity, Submission
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.outbox import OutboxRepository
from run_agent_gateway import AgentGateway, GatewayScheduler, IdentityPolicy, IdentityRule
from run_agent_gateway import InboundMessage, QueueGatewayAdapter
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.controller import SessionController
scripts = {e.name:e.value for e in distribution('run-agent-harness').entry_points
           if e.group == 'console_scripts'}
assert scripts == {'run':'run_agent_entry:main'}, scripts
async def check():
    async with await SqliteDatabase.open('state.sqlite3') as db:
        repo = SqliteSessionRepository(db)
        await repo.create_session(cwd='.', principal_id='local', model='test', session_id='s')
        token = await repo.claim('s', owner_id='host', run_id='run')
        await repo.append_entries([MessageEntry(id='a', message=UserMessage(content='persist'))],
                                  token=token, expected_head=None)
    async with await SqliteDatabase.open('state.sqlite3') as db:
        assert (await SqliteSessionRepository(db).get_head('s')).entry_id == 'a'
        sink = SqliteTelemetrySink(db)
        await sink.append('accounting', {'cost':0.5})
        assert (await sink.read('accounting'))[0]['cost'] == 0.5
        await sink.aclose()
        gateway = GatewayRepository(db)
        await gateway.initialize()
        owner = await gateway.acquire_owner('installed-host')
        submitted = Submission(RouteIdentity('installed','account','chat'), 'local',
                               'message-one', 'test admission', pathlib.Path.cwd())
        accepted = await gateway.admit(owner, submitted, model='test')
        assert (await gateway.admit(owner, submitted, model='test')).task_id == accepted.task_id
        assignment = await gateway.claim_next(owner)
        await gateway.complete(owner, assignment, status='succeeded', output='verified')
        await gateway.release(owner, assignment)
        outbox = OutboxRepository(gateway)
        for kind in ['accepted', 'result']:
            delivery = (await outbox.claim(owner))[0]
            assert delivery.kind == kind
            await outbox.acknowledge(owner, delivery, {'id':kind})
        await gateway.release_owner(owner)
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(reason='stop', message=AssistantMessage(
                content=[TextContent(text='installed-wheel')], model='test', stop_reason='stop'))
    started, finish = asyncio.Event(), asyncio.Event()
    class SteeringProvider(Provider):
        async def stream_response(self, **kwargs):
            if not any(message.text.startswith('Create a concise session name')
                       for message in kwargs['messages']) and not started.is_set():
                started.set()
                await finish.wait()
            async for event in super().stream_response(**kwargs):
                yield event
    gateway_paths = RunAgentPaths(home=pathlib.Path('gateway'), agents_home=pathlib.Path('agents'))
    gateway_options = ApplicationOptions(cwd=pathlib.Path.cwd(), paths=gateway_paths,
        model='test', provider_name='test', extensions_enabled=False)
    async with await SqliteDatabase.open(gateway_paths.database_path) as database:
        repository = GatewayRepository(database)
        await repository.initialize()
        owner = await repository.acquire_owner('installed-runtime')
        runtime = GatewayCodingRuntime(repository, owner, gateway_options,
                                       provider_factory=lambda _: SteeringProvider())
        scheduler = GatewayScheduler(repository, owner, CodingAssignmentRunner(runtime))
        adapter = QueueGatewayAdapter('installed')
        policy = IdentityPolicy((IdentityRule('installed','account','local','local',
                                              pathlib.Path.cwd()),))
        host = AgentGateway(scheduler, [adapter], policy, model='test')
        await host.start()
        try:
            await adapter.receive_message(InboundMessage('runtime-message','account',
                                                         'local','chat','installed prompt'))
            accepted = await asyncio.wait_for(adapter.next_sent(), 5)
            await asyncio.wait_for(started.wait(), 5)
            await adapter.receive_message(InboundMessage('steer-message','account',
                                                         'local','chat','/steer correction'))
            steering = await asyncio.wait_for(adapter.next_sent(), 5)
            assert steering.content['mode'] == 'steer'
            finish.set()
            deliveries = [await asyncio.wait_for(adapter.next_sent(), 5) for _ in range(2)]
            consumed = next(item for item in deliveries if item.task_id == steering.task_id)
            result = next(item for item in deliveries if item.task_id == accepted.task_id)
            assert consumed.content['status'] == 'consumed'
            assert accepted.content['status'] == 'accepted'
            assert result.content['output'] == 'installed-wheel'
            assert result.task_id == accepted.task_id
            assert not scheduler.errors
        finally:
            finish.set()
            await host.shutdown()
    paths = RunAgentPaths(home=pathlib.Path('application'), agents_home=pathlib.Path('agents'))
    skill_root = paths.home / 'skills' / 'installed'
    skill_root.mkdir(parents=True)
    (skill_root / 'SKILL.md').write_text('Installed skill v1', encoding='utf-8')
    (skill_root / 'helper.py').write_text("print('fixed helper')", encoding='utf-8')
    extension = pathlib.Path('installed_extension.py').resolve()
    extension.write_text(chr(10).join([
        'from pathlib import Path',
        'from run_agent_coding.extensions import ResourceSelection',
        'def setup(api):',
        '    def resources(view):',
        '        return [ResourceSelection("project", key, version, key)',
        '                for key, version in view.heads("project").items()]',
        '    api.register_resource_provider("notes", resources, version="1")',
        '    async def cleanup():',
        '        Path(__file__).with_suffix(".closed").touch()',
        '    api.register_disposer(cleanup)',
    ]), encoding='utf-8')
    options = ApplicationOptions(cwd=pathlib.Path.cwd(), paths=paths, model='test',
                                 extensions_enabled=False, extension_paths=(extension,))
    async with await CodingApplication.open(options, provider=Provider()) as app:
        arguments = [sys.executable, '-c', 'print(12345)']
        command = subprocess.list2cmdline(arguments) if os.name == 'nt' else shlex.join(arguments)
        terminal = await app.session.run_terminal_command(command, add_to_context=False)
        assert terminal.ok and terminal.output.strip() == '12345'
        assert app.session._processes.active_count == 0
        events = [event async for event in app.prompt('persist installed session')]
        assert events[-1].status == 'succeeded'
        identity = app.session.session_id
        head = events[-1].head_id
        runtime = app.session.extension_runtime
        snapshot = await runtime._extensions[0].api.context.services.snapshots.read(
            events[-1].snapshot_id)
        assert snapshot.payload['purpose'] == 'agent'
        assert snapshot.payload['resource_snapshot_id'] == app.session._resource_snapshot_id
        original_skill = app.session.skills[0]
        assert original_skill.package_digest in str(original_skill.path)
        (skill_root / 'SKILL.md').write_text('Installed skill v2', encoding='utf-8')
        assert original_skill.path.read_text(encoding='utf-8') == 'Installed skill v1'
        state = runtime._extensions[0].api.context.services.scope().state
        await state.compare_and_set(StateChange('installed', 0, True))
        resources = runtime._extensions[0].api.context.services.scope('project').resources
        value = await resources.put_immutable('MEMORY.md', 'installed memory version one')
        await resources.advance_head(HeadChange('MEMORY.md', None, value.version, 'check', {}))
        await app.command('/reload')
        assert extension.with_suffix('.closed').exists()
        runtime = app.session.extension_runtime
        current_state = runtime._extensions[0].api.context.services.scope().state
        assert (await current_state.get('installed')).value
        assert app.session.skills[0].content == 'Installed skill v2'
        assert 'installed memory version one' in app.session.system_prompt
        resources = runtime._extensions[0].api.context.services.scope('project').resources
        newer = await resources.put_immutable('MEMORY.md', 'installed memory version two')
        await resources.advance_head(HeadChange('MEMORY.md', value.version, newer.version,
                                               'check', {}))
        head = (await app.session.storage.get_head()).entry_id
        skill_version = app.session.skills[0].package_digest
    (skill_root / 'SKILL.md').write_text('Unloaded skill v3', encoding='utf-8')
    from dataclasses import replace
    reopened = await CodingApplication.open(replace(options, resume=identity), provider=Provider())
    async with reopened as app:
        assert (await app.session.storage.get_head()).entry_id == head
        assert app.session.skills[0].package_digest == skill_version
        assert app.session.skills[0].content == 'Installed skill v2'
        await app.start()
        assert 'installed memory version one' in app.session.system_prompt
        assert 'installed memory version two' not in app.session.system_prompt
    assert not any(name.startswith(('textual', 'run_agent_coding.tui')) for name in sys.modules)
    assert not list(pathlib.Path.cwd().rglob('*.jsonl'))
asyncio.run(check())
print(json.dumps({'entry_module':run_agent_entry.__file__, 'scripts':scripts,
                  'schema_initialization_and_reopen':True,
                  'application_completion_and_resume':True,
                  'sqlite_telemetry':True, 'no_jsonl_output':True,
                  'host_services_and_reload':True, 'context_snapshot':True,
                  'skill_package_and_resume':True, 'extension_disposer':True,
                  'extension_resource_capture_reload_resume':True,
                  'gateway_transactions':True, 'gateway_live_coding_and_outbox':True,
                  'gateway_steering_consumed':True}))
"""


GATEWAY_ADAPTER = """
import asyncio, json, os
from pathlib import Path
from run_agent_gateway import InboundMessage

class Adapter:
    name = 'installed-cli'

    def __init__(self):
        self.result = asyncio.Event()
        self.status = asyncio.Event()
        self.deliveries = []

    async def messages(self):
        index = os.environ['GATEWAY_TEST_INDEX']
        if index == '2':
            yield InboundMessage('cli-1', 'account', 'sender', 'chat', 'prompt 1')
        chat = 'background-chat' if index == '3' else 'chat'
        prompt = '/background isolated prompt' if index == '3' else 'prompt ' + index
        yield InboundMessage('cli-' + index, 'account', 'sender', chat, prompt)
        await asyncio.wait_for(self.result.wait(), 15)
        yield InboundMessage('status-' + index, 'account', 'sender', chat, '/status')
        await asyncio.wait_for(self.status.wait(), 5)

    async def send(self, delivery):
        self.deliveries.append({'task_id':delivery.task_id, 'kind':delivery.kind,
                                'content':delivery.content})
        if delivery.kind == 'result':
            self.result.set()
        if delivery.content.get('status') == 'status':
            self.status.set()
        return {'id':delivery.delivery_id}

    async def close(self):
        Path(os.environ['GATEWAY_TEST_REPORT']).write_text(json.dumps(self.deliveries),
                                                         encoding='utf-8')

def setup_gateway(api):
    api.register_adapter(Adapter())
"""


def check_gateway_cli(launcher: Path, directory: Path) -> dict[str, object]:
    requests: list[object] = []
    workspace = directory / "cli-workspace"
    workspace.mkdir()
    (workspace / "tracked.txt").write_text("clean source\n", encoding="utf-8")
    for arguments in (["init"], ["add", "tracked.txt"], ["commit", "-m", "fixture"]):
        subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "user.name=Distribution Fixture",
             "-c", "user.email=fixture@example.invalid", "-C", str(workspace), *arguments],
            capture_output=True, check=True, timeout=15,
        )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            chunks = [
                {"type": "response.output_text.delta", "delta": "offline gateway reply"},
                {"type": "response.completed", "response": {
                    "status": "completed", "output": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                }},
            ]
            if not self.path.endswith("/responses"):
                chunks = [{"id": "offline", "model": "gpt-4o-mini", "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "offline gateway reply"},
                     "finish_reason": "stop"}
                ], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, format: str, *args: object) -> None:
            pass

    adapter = directory / "gateway_adapter.py"
    adapter.write_text(GATEWAY_ADAPTER, encoding="utf-8")
    identities = directory / "identities.json"
    identities.write_text(json.dumps([{
        "adapter_instance_id": "installed-cli", "account_id": "account",
        "sender_id": "sender", "principal_id": "local", "workspace": str(workspace),
    }]), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT",
        "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
    }}
    env.update({
        "OPENAI_API_KEY": "offline-key",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "PYTHONIOENCODING": "utf-8",
    })
    state = directory / "cli-gateway-state"
    try:
        for index in (1, 2, 3):
            report = directory / f"gateway-deliveries-{index}.json"
            result = subprocess.run(
                [str(launcher), "gateway", "--extension", str(adapter),
                 "--identity-map", str(identities), "--state-dir", str(state),
                 "--cwd", str(directory), "--provider", "openai", "--model", "gpt-4o-mini"],
                cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=30,
                env={**env, "GATEWAY_TEST_INDEX": str(index), "GATEWAY_TEST_REPORT": str(report)},
            )
            assert result.returncode == 0, result.stdout + result.stderr
            deliveries = json.loads(report.read_text(encoding="utf-8"))
            assert [d["kind"] for d in deliveries] == ["accepted", "result", "control"]
            assert deliveries[1]["content"]["output"] == "offline gateway reply"
            assert len(deliveries[2]["content"]["tasks"]) == index
            if index == 3:
                assert deliveries[1]["content"]["lane"] == "background"
                assert deliveries[1]["content"]["artifacts"]["manifest"]
                assert (deliveries[1]["content"]["origin_session_id"]
                        != deliveries[1]["content"]["session_id"])
        with closing(sqlite3.connect(state / "state.sqlite3")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM gateway_tasks").fetchone()[0] == 3
            assert connection.execute("SELECT COUNT(*) FROM gateway_routes").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
            assert connection.execute(
                "SELECT COUNT(*) FROM gateway_tasks WHERE status='succeeded'"
            ).fetchone()[0] == 3
            assert connection.execute(
                "SELECT COUNT(*) FROM gateway_outbox WHERE status!='sent'"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM gateway_attempts WHERE released=0"
            ).fetchone()[0] == 0
        assert any("prompt 1" in json.dumps(body) and "prompt 2" in json.dumps(body)
                   for body in requests)
        assert not list(directory.rglob("*.jsonl"))
        return {"launches": 3, "tasks": 3, "sessions": 3, "all_deliveries_sent": True,
                "duplicate_on_restart": True, "resumed_context": True,
                "background_first_input_worktree_and_artifacts": True,
                "provider": "local HTTP fixture; no real model"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    python, launcher = args.python.resolve(), args.run.resolve()
    with tempfile.TemporaryDirectory(prefix="run-distribution-check-") as directory:
        smoke = subprocess.run(
            [str(python), "-c", SMOKE],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        report = json.loads(smoke.stdout)
        assert Path(report["entry_module"]).is_relative_to(python.parent.parent)
        report["launcher"] = str(launcher)
        report["commands"] = []
        for argv in [["--version"], ["--help"], ["gateway", "--help"],
                     ["gateway", "recover", "--help"], ["bench", "--help"]]:
            result = subprocess.run(
                [str(launcher), *argv],
                cwd=directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=True,
            )
            report["commands"].append(
                {
                    "args": argv,
                    "exit_code": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                    "stderr": result.stderr,
                }
            )
        for obsolete in ["run-agent", "run-agent-gateway", "run-agent-bench"]:
            assert not launcher.with_name(obsolete + launcher.suffix).exists()
        report["gateway_cli"] = check_gateway_cli(launcher, Path(directory))
        inspection = subprocess.run(
            [str(launcher), "gateway", "recover", "--state-dir", str(Path(directory) / "gateway")],
            cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        )
        assert json.loads(inspection.stdout) == []
        report["gateway_recovery_inspection"] = True
        refreshed = subprocess.run(
            [str(launcher), "--refresh-resources", "--print", "unused"],
            cwd=directory, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert refreshed.returncode != 0 and "requires --session" in refreshed.stderr
        report["refresh_resources_requires_session"] = True
    report["scope"] = (
        "Wheel import, schema, persistence, command routing, application completion and resume; "
        "terminal interactions and real model execution are separate gates."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        "Clean installation: command routes, one launcher, schema initialization and reopen passed."
    )


if __name__ == "__main__":
    main()
