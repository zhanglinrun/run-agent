"""CLI acceptance.

- The Coding CLI manages memory in an isolated process.
- Redirected stdin without --print is refused instead of waiting for input, and
  Ctrl+C maps to exit code 130. "No control codes in machine output" is asserted
  by test_print_sqlite.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.text import Text

import run_agent_entry
from run_agent_coding import cli

REPO = Path(__file__).resolve().parents[2]

MEMORY_PROBE = """
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
STATE = Path(sys.argv[2])
sys.path.insert(0, str(REPO))

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.paths import RunAgentPaths
from tests.redesign.test_coding_application import ReplyProvider


def probe_options():
    return ApplicationOptions(
        cwd=REPO,
        paths=RunAgentPaths(home=STATE, agents_home=STATE / "agents"),
        model="test",
        provider_name="test",
        extension_paths=(REPO / "src" / "run_agent_extensions" / "hermes_memory",),
        extensions_enabled=True,
    )


async def main():
    app = await CodingApplication.open(probe_options(), provider=ReplyProvider())
    await app.start()
    result = await app.command("/memory show")
    tools = sorted(tool.name for tool in app.session.extension_runtime.extension_tools)
    await app.aclose()
    print(json.dumps({"handled": result.handled, "tools": tools}))


asyncio.run(main())
"""


def test_coding_cli_manages_memory_in_an_isolated_process(tmp_path):
    probe = tmp_path / "memory_probe.py"
    probe.write_text(MEMORY_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(probe), str(REPO), str(tmp_path / "state")],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["handled"] is True
    assert "memory" in payload["tools"]


def test_redirected_stdin_without_print_is_refused_not_awaited(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "run_agent_entry",
            "--no-extensions",
            "--state-dir",
            str(tmp_path / "state"),
            "hello",
        ],
        input="",
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        timeout=30,
    )
    assert completed.returncode == 2, (completed.returncode, completed.stdout, completed.stderr)
    assert "--print" in Text.from_ansi(completed.stderr).plain


def test_keyboard_interrupt_maps_to_exit_code_130(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "app", interrupted)
    assert run_agent_entry.main([]) == 130


def test_version_still_exits_zero_after_the_interrupt_mapping():
    assert run_agent_entry.main(["--version"]) == 0


@pytest.mark.parametrize("obsolete", ["run-agent", "run-agent-bench"])
def test_no_obsolete_console_script_is_registered(obsolete):
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        distribution("run-agent-harness")
    except PackageNotFoundError:
        pytest.skip("package metadata is unavailable in this environment")
    from importlib.metadata import entry_points

    names = {point.name for point in entry_points(group="console_scripts")}
    assert obsolete not in names
