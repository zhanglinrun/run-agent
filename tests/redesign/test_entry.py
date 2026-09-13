"""The installed command selects one host without importing the other hosts."""

import subprocess
import sys
from importlib.metadata import distribution


def test_console_metadata_has_one_entry():
    scripts = {
        entry.name: entry.value
        for entry in distribution("run-agent-harness").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts == {"run": "run_agent_entry:main"}


def test_version_is_available_without_loading_business_layers():
    code = """
import sys
from run_agent_entry import main
assert main(['--version']) == 0
assert not any(name.startswith(('run_agent_coding', 'run_agent_gateway', 'run_agent_evals'))
               for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Run Agent ")


def test_storage_import_does_not_load_sessions_or_frontends():
    code = """
import sys
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.resources import NamespaceResources
assert 'run_agent_coding.session' not in sys.modules
assert 'run_agent_coding.cli' not in sys.modules
assert not any(name.startswith(('run_agent_gateway', 'run_agent_evals')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_host_help_routes():
    for args in (["--help"], ["gateway", "--help"], ["bench", "--help"]):
        result = subprocess.run(
            [sys.executable, "-m", "run_agent_entry", *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert "usage" in result.stdout.lower()


def test_invalid_option_reports_usage_without_traceback():
    result = subprocess.run(
        [sys.executable, "-m", "run_agent_entry", "--mode", "unsupported"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "unsupported" in result.stderr
