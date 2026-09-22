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
from types import SimpleNamespace

import pytest

from run_agent_extensions.experience.commands_evolution import (
    EVOLVE_USAGE,
    register_evolution_commands,
)

REPO = Path(__file__).resolve().parents[2]
OBSOLETE = ("run-agent", "run-agent-bench")
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


EVOLVE_ACTIONS = {
    "status",
    "candidates",
    "show",
    "propose",
    "adopt",
    "publish",
    "reject",
    "ledger",
    "rollback",
}


def documented_evolve_actions() -> set[str]:
    """Every ``/evolve <action>`` the shipped usage text advertises."""
    actions: set[str] = set()
    for chunk in re.findall(r"/evolve ([^;]+)", EVOLVE_USAGE):
        head = chunk.split("<")[0].split("[")[0]
        actions.update(re.findall(r"[a-z]+", head))
    return actions


class RecordingCommandAPI:
    """Capture the commands one extension registers, without an application."""

    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def register_command(self, name: str, handler: object, *, description: str = "") -> None:
        del description
        self.commands[name] = handler


def stub_evolution() -> object:
    """The smallest SkillEvolution surface ``/evolve`` reads before refusing."""
    candidate = SimpleNamespace(
        candidate_id="candidate-1",
        status="cold",
        scope="user",
        name="config-skill",
        source_session="session",
        source_run="run",
        base_digest=None,
        candidate_digest="a" * 64,
        report_id=None,
        operations=(),
        claims=(),
    )
    candidates = SimpleNamespace(
        list=lambda status=None: [],
        require=lambda candidate_id: candidate,
        content=lambda item: "body",
    )
    ledger = SimpleNamespace(entries=lambda **kwargs: [], get=lambda entry_id: None)

    async def propose_from_run(**kwargs: object) -> object:
        return candidate

    async def publish(candidate_id: str) -> object:
        return SimpleNamespace(message=f"published {candidate_id}")

    return SimpleNamespace(
        status_text=lambda: "evolution evaluation: available",
        candidates=candidates,
        skills=SimpleNamespace(ledger={"user": ledger, "project": ledger}),
        config=SimpleNamespace(skills_write_approval=False),
        propose_from_run=propose_from_run,
        adopt=lambda scope, name: SimpleNamespace(message="adopted"),
        publish=publish,
        reject=lambda candidate_id, reason: SimpleNamespace(candidate_id=candidate_id),
    )


@pytest.mark.anyio
async def test_every_documented_evolve_subcommand_is_registered_and_handled() -> None:
    """``/evolve`` recognizes every action its own usage text advertises."""
    api = RecordingCommandAPI()
    register_evolution_commands(api, stub_evolution)  # type: ignore[arg-type]
    handler = api.commands["evolve"]

    invocations = {
        "status": "status",
        "candidates": "candidates",
        "show": "show some-id",
        "propose": "propose config-skill --session session --run run",
        "adopt": "adopt config-skill",
        "publish": "publish some-id",
        "reject": "reject some-id",
        "ledger": "ledger",
        "rollback": "rollback some-id",
    }
    assert set(invocations) == EVOLVE_ACTIONS
    assert documented_evolve_actions() == EVOLVE_ACTIONS

    context = SimpleNamespace(
        api=SimpleNamespace(context=SimpleNamespace(has_ui=False, ui=SimpleNamespace(confirm=None)))
    )
    for action, arguments in invocations.items():
        result = await handler(arguments, context)
        assert result != EVOLVE_USAGE, f"/evolve {action} is documented but not handled"
