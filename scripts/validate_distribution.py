"""Verify a clean wheel installation outside the repository and without a model."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

SMOKE = """
import asyncio, json, pathlib
from importlib.metadata import distribution
import run_agent_entry
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_core.session.entries import MessageEntry
from run_agent_core.messages import UserMessage
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
asyncio.run(check())
print(json.dumps({'entry_module':run_agent_entry.__file__, 'scripts':scripts,
                  'schema_initialization_and_reopen':True}))
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
        "Wheel import, schema, persistence, command routing; "
        "interactive and model execution are separate gates."
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        "Clean installation: command routes, one launcher, schema initialization and reopen passed."
    )


if __name__ == "__main__":
    main()
