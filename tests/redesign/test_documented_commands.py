"""The documented command surface matches the shipped one.

Only ``run`` is registered, so no document may present an obsolete console
script as a command; every ``run bench <sub>`` example must name a subcommand
that actually exists; and every bundled example extension must load and expose
its entry point.

The scan keys on ``<name>.exe``, so prose that merely names a script does not
trip it.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OBSOLETE = ("run-agent", "run-agent-gateway", "run-agent-bench")
TEXT_SUFFIXES = {".md", ".py", ".toml", ".ps1", ".sh", ".yml", ".yaml", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {".git", ".venv", ".run", "__pycache__"}


def tracked_text_files() -> list[Path]:
    """Every tracked text file that documentation examples could live in."""
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True, timeout=60
    )
    paths = []
    for name in listed.stdout.splitlines():
        path = REPO / name
        if path.suffix not in TEXT_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in Path(name).parts):
            continue
        paths.append(path)
    return paths


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_no_document_presents_an_obsolete_console_script_as_a_command():
    offenders = [
        f"{path.relative_to(REPO)}: {name}.exe"
        for path in tracked_text_files()
        for name in OBSOLETE
        if f"{name}.exe" in read(path)
    ]
    assert offenders == []


def documented_bench_subcommands() -> set[str]:
    found: set[str] = set()
    for path in tracked_text_files():
        if path.suffix != ".md":
            continue
        found.update(re.findall(r"run(?:\.exe)? bench ([a-z][a-z-]*)", read(path)))
    return found


def test_every_documented_bench_subcommand_exists():
    documented = documented_bench_subcommands()
    assert documented, "no bench subcommand is documented anywhere"
    completed = subprocess.run(
        [sys.executable, "-m", "run_agent_entry", "bench", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    for name in sorted(documented):
        assert name in completed.stdout, f"documented but missing from the CLI: run bench {name}"


def test_the_eval_documentation_does_not_advertise_removed_jsonl_output():
    text = read(REPO / "evals" / "README.md")
    assert "runtime/calls/" not in text
    assert "runtime/traces/" not in text


def example_modules() -> list[Path]:
    bundled = REPO / "src" / "run_agent_coding" / "data" / "examples"
    return sorted(bundled.rglob("*.py"))


@pytest.mark.parametrize("path", example_modules(), ids=lambda item: item.name)
def test_example_extension_loads_and_exposes_its_entry_point(path: Path):
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, "setup", None)), f"{path.name} has no setup()"
