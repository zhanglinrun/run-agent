"""Verify a clean wheel installation outside the repository and without a model."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.text import Text

SMOKE = """
import asyncio, json, os, pathlib, shlex, subprocess, sys
from importlib.metadata import distribution
import run_agent_entry
from run_agent_coding.session_manager import SessionManager
from run_agent_core.session.entries import MessageEntry
from run_agent_core.messages import UserMessage
from run_agent_coding.application import CodingApplication, ApplicationOptions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.host.contracts import StateChange, HeadChange
from run_agent_coding.storage.telemetry import JsonlTelemetrySink
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantDoneEvent
scripts = {e.name:e.value for e in distribution('run-agent-harness').entry_points
           if e.group == 'console_scripts'}
assert scripts == {'run':'run_agent_entry:main'}, scripts
async def check():
    paths = RunAgentPaths(home=pathlib.Path('state'), agents_home=pathlib.Path('agents'))
    manager = SessionManager(paths)
    record = await manager.create_session(cwd=pathlib.Path('.'), model='test', session_id='s')
    writer = await manager.open_storage(record.id)
    await writer.append_entries([MessageEntry(id='a', message=UserMessage(content='persist'))],
                                token=writer.token, expected_head=None)
    await writer.aclose()
    await manager.aclose()
    manager = SessionManager(paths)
    writer = await manager.open_storage('s')
    assert (await writer.get_head()).entry_id == 'a'
    sink = JsonlTelemetrySink(paths.logs_dir / 'observations.jsonl')
    await sink.append('accounting', {'cost':0.5})
    assert (await sink.read('accounting'))[0]['cost'] == 0.5
    await sink.aclose()
    await manager.aclose()
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
        snapshot = await app.session.storage.get_snapshot(events[-1].snapshot_id)
        assert snapshot['payload']['purpose'] == 'agent'
        assert snapshot['payload']['resource_snapshot_id'] == app.session._resource_snapshot_id
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
    assert list(pathlib.Path.cwd().rglob('*.sqlite3')) == []
asyncio.run(check())
print(json.dumps({'entry_module':run_agent_entry.__file__, 'python_prefix':sys.prefix,
                  'scripts':scripts,
                  'schema_initialization_and_reopen':True,
                  'application_completion_and_resume':True,
                  'jsonl_telemetry':True, 'no_sqlite_output':True,
                  'host_services_and_reload':True, 'context_snapshot':True,
                  'skill_package_and_resume':True, 'extension_disposer':True,
                  'extension_resource_capture_reload_resume':True}))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Resolving a venv's Python symlink would select the global interpreter on POSIX.
    python, launcher = args.python.absolute(), args.run.absolute()
    with tempfile.TemporaryDirectory(prefix="run-distribution-check-") as directory:
        smoke = subprocess.run(
            [str(python), "-c", SMOKE],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if smoke.returncode:
            sys.stderr.write(smoke.stderr)
            raise SystemExit(smoke.returncode)
        report = json.loads(smoke.stdout)
        prefix = Path(report["python_prefix"]).resolve()
        assert prefix == python.parent.parent.resolve()
        assert Path(report["entry_module"]).resolve().is_relative_to(prefix)
        report["launcher"] = str(launcher)
        report["commands"] = []
        for argv in [
            ["--version"],
            ["--help"],
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
        for obsolete in ["run-agent", "run-agent-bench"]:
            assert not launcher.with_name(obsolete + launcher.suffix).exists()
        refreshed = subprocess.run(
            [str(launcher), "--refresh-resources", "--print", "unused"],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert (
            refreshed.returncode != 0
            and "requires --session" in Text.from_ansi(refreshed.stderr).plain
        )
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
