"""Verify a clean wheel installation outside the repository and without a model."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
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
from run_agent_gateway import BasePlatformAdapter, FeishuConfig, GatewayConfig
from run_agent_gateway import GatewayRunner, MessageEvent, SendResult, SessionSource
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
    class Provider:
        async def stream_response(self, **kwargs):
            yield AssistantDoneEvent(reason='stop', message=AssistantMessage(
                content=[TextContent(text='installed-wheel')], model='test', stop_reason='stop'))
    class Adapter(BasePlatformAdapter):
        name = 'installed'
        def __init__(self):
            super().__init__()
            self.sent = []
        async def connect(self):
            return True
        async def disconnect(self):
            await self.cancel_background_tasks()
        async def send(self, chat_id, content, *, reply_to=None, thread_id=None):
            self.sent.append((chat_id, content, reply_to))
            return SendResult(success=True, message_id=str(len(self.sent)))
    gateway_paths = RunAgentPaths(home=pathlib.Path('gateway'), agents_home=pathlib.Path('agents'))
    gateway_options = ApplicationOptions(cwd=pathlib.Path.cwd(), paths=gateway_paths,
        model='test', provider_name='test', extensions_enabled=False)
    config = GatewayConfig(feishu=FeishuConfig('app', 'secret', allowed_users=frozenset({'ou_1'})))
    adapter = Adapter()
    runner = GatewayRunner(config, gateway_options, adapter, provider_factory=lambda: Provider())
    await runner.start()
    try:
        source = SessionSource('installed', 'chat', 'dm', user_id='ou_1', user_name='One')
        await adapter.handle_message(MessageEvent('installed prompt', source, message_id='m1'))
        await adapter.wait_idle()
        assert adapter.sent == [('chat', 'installed-wheel', 'm1')], adapter.sent
        first = runner.store.get('installed:dm:chat').session_id
        await adapter.handle_message(MessageEvent('/new', source, message_id='m2'))
        await adapter.wait_idle()
        assert runner.store.get('installed:dm:chat').session_id != first
        await adapter.handle_message(MessageEvent('again', source, message_id='m3'))
        await adapter.wait_idle()
        assert adapter.sent[-1][1] == 'installed-wheel'
        assert (gateway_paths.home / 'gateway' / 'sessions.json').is_file()
    finally:
        await runner.stop()
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
    assert not list(pathlib.Path.cwd().rglob('*.jsonl'))
asyncio.run(check())
print(json.dumps({'entry_module':run_agent_entry.__file__, 'scripts':scripts,
                  'schema_initialization_and_reopen':True,
                  'application_completion_and_resume':True,
                  'sqlite_telemetry':True, 'no_jsonl_output':True,
                  'host_services_and_reload':True, 'context_snapshot':True,
                  'skill_package_and_resume':True, 'extension_disposer':True,
                  'extension_resource_capture_reload_resume':True,
                  'gateway_runner_session_store_and_reply':True}))
"""


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
        for argv in [
            ["--version"],
            ["--help"],
            ["gateway", "--help"],
            ["bench", "--help"],
        ]:
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
        missing = subprocess.run(
            [str(launcher), "gateway", "--cwd", directory],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env={k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")},
        )
        assert missing.returncode != 0 and "FEISHU_APP_ID" in missing.stderr, missing.stderr
        report["gateway_requires_feishu_credentials"] = True
        refreshed = subprocess.run(
            [str(launcher), "--refresh-resources", "--print", "unused"],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
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
