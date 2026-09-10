"""Verify a clean wheel installation outside the repository and without a model."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

SMOKE = """
import asyncio, json, pathlib, sys
from importlib.metadata import distribution
import run_agent_entry
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.telemetry import SqliteTelemetrySink
from run_agent_core.session.entries import MessageEntry
from run_agent_core.messages import UserMessage
from run_agent_coding.application import CodingApplication, ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.host.contracts import StateChange
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
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
    paths = RunAgentPaths(home=pathlib.Path('application'), agents_home=pathlib.Path('agents'))
    skill_root = paths.home / 'skills' / 'installed'
    skill_root.mkdir(parents=True)
    (skill_root / 'SKILL.md').write_text('Installed skill v1', encoding='utf-8')
    (skill_root / 'helper.py').write_text("print('fixed helper')", encoding='utf-8')
    extension = pathlib.Path('installed_extension.py').resolve()
    extension.write_text(chr(10).join([
        'from pathlib import Path',
        'def setup(api):',
        '    async def cleanup():',
        '        Path(__file__).with_suffix(".closed").touch()',
        '    api.register_disposer(cleanup)',
    ]), encoding='utf-8')
    options = ApplicationOptions(cwd=pathlib.Path.cwd(), paths=paths, model='test',
                                 extensions_enabled=False, extension_paths=(extension,))
    async with await CodingApplication.open(options, provider=Provider()) as app:
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
        await app.command('/reload')
        assert extension.with_suffix('.closed').exists()
        runtime = app.session.extension_runtime
        current_state = runtime._extensions[0].api.context.services.scope().state
        assert (await current_state.get('installed')).value
        assert app.session.skills[0].content == 'Installed skill v2'
        head = (await app.session.storage.get_head()).entry_id
        skill_version = app.session.skills[0].package_digest
    (skill_root / 'SKILL.md').write_text('Unloaded skill v3', encoding='utf-8')
    from dataclasses import replace
    reopened = await CodingApplication.open(replace(options, resume=identity), provider=Provider())
    async with reopened as app:
        assert (await app.session.storage.get_head()).entry_id == head
        assert app.session.skills[0].package_digest == skill_version
        assert app.session.skills[0].content == 'Installed skill v2'
    assert not any(name.startswith(('textual', 'run_agent_coding.tui')) for name in sys.modules)
    assert not list(pathlib.Path.cwd().rglob('*.jsonl'))
asyncio.run(check())
print(json.dumps({'entry_module':run_agent_entry.__file__, 'scripts':scripts,
                  'schema_initialization_and_reopen':True,
                  'application_completion_and_resume':True,
                  'sqlite_telemetry':True, 'no_jsonl_output':True,
                  'host_services_and_reload':True, 'context_snapshot':True,
                  'skill_package_and_resume':True, 'extension_disposer':True}))
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
        for argv in [["--version"], ["--help"], ["gateway", "--help"], ["bench", "--help"]]:
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
