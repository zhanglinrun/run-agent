"""Select the tests affected by the current working-tree change set.

The fast path is a convenience, never a gate: it escalates to the full suite
whenever the change could plausibly affect code the mapping cannot see.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

RISKY_PREFIXES = (
    "src/run_agent_core/",
    "src/run_agent_coding/storage/",
    "src/run_agent_coding/extensions/",
    "src/run_agent_coding/host/",
    "extensions/",
    "tests/",
    "pyproject.toml",
)
RISKY_FILES = (
    "src/run_agent_entry.py",
    "src/run_agent_coding/session.py",
    "src/run_agent_coding/application.py",
    "src/run_agent_coding/cli.py",
)
SOURCE_ROOTS = ("src/", "extensions/")


@dataclass(frozen=True)
class Selection:
    """The changed files, the tests chosen for them, and why."""

    changed: tuple[str, ...]
    tests: tuple[str, ...] | None
    notes: tuple[str, ...]

    @property
    def escalated(self) -> bool:
        return self.tests is None

    def pytest_argv(self, python: str) -> tuple[str, ...]:
        if self.tests is None:
            return (python, "-m", "pytest", "tests/redesign", "-q")
        return (python, "-m", "pytest", "-q", *self.tests)


def git(root: Path, *args: str) -> str:
    """Return git stdout for args, or an empty string when git fails."""
    result = subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout if result.returncode == 0 else ""


def changed_files(root: Path) -> tuple[str, ...]:
    """List working-tree files that differ from HEAD, ignoring deletions."""
    names: set[str] = set()
    for line in git(root, "status", "--porcelain").splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2].strip(), line[3:].strip().strip('"')
        if status == "D":
            continue
        path = path.split(" -> ", 1)[-1]
        names.add(path.replace("\\", "/"))
    return tuple(sorted(names))


def is_risky(path: str) -> bool:
    """True when the file has blast radius the test mapping cannot bound."""
    return path.startswith(RISKY_PREFIXES) or path in RISKY_FILES


def module_name(path: str) -> str | None:
    """Convert a source path into its dotted module name, if it has one."""
    if not path.endswith(".py") or not path.startswith(SOURCE_ROOTS):
        return None
    parts = list(Path(path).parts)
    if parts[0] == "src":
        parts = parts[1:]
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def tests_for(modules: tuple[str, ...], tests_dir: Path) -> tuple[str, ...]:
    """Find test modules that reference any of the changed modules."""
    parents = {module.rsplit(".", 1)[0] for module in modules if "." in module}
    needles = tuple(modules) + tuple(sorted(parents))
    chosen = [
        str(test.relative_to(tests_dir.parent.parent)).replace("\\", "/")
        for test in sorted(tests_dir.glob("test_*.py"))
        if any(needle in test.read_text(encoding="utf-8") for needle in needles)
    ]
    return tuple(chosen)


def lint_targets(changed: tuple[str, ...]) -> tuple[str, ...]:
    """Paths ruff should receive directly; anything else falls back to the whole repo.

    Markdown is included because ruff formats Python code fences inside it.
    """
    selected = tuple(path for path in changed if path.endswith((".py", ".pyi", ".md")))
    return selected or (".",)


def select(root: Path) -> Selection:
    """Choose the affected test subset, falling back to the full suite."""
    changed = changed_files(root)
    notes = [f"changed files: {len(changed)}"]
    if not changed:
        notes.append("no changes detected; falling back to the full suite")
        return Selection(changed, None, tuple(notes))
    if any(is_risky(path) for path in changed):
        notes.append("change touches high blast radius paths; falling back to the full suite")
        return Selection(changed, None, tuple(notes))
    modules = tuple(name for name in (module_name(p) for p in changed) if name)
    if not modules:
        notes.append("no importable source module changed; falling back to the full suite")
        return Selection(changed, None, tuple(notes))
    tests = tests_for(modules, root / "tests" / "redesign")
    if not tests:
        notes.append("no test references the changed modules; falling back to the full suite")
        return Selection(changed, None, tuple(notes))
    notes.append(f"changed modules: {', '.join(modules)}")
    notes.append(f"affected tests: {', '.join(Path(t).name for t in tests)}")
    return Selection(changed, tests, tuple(notes))
