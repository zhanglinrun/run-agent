"""Discover deterministic verification commands from repository files."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sys
from typing import Iterable


@dataclass(frozen=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 120.0
    focused: bool = False


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def discover_verification_commands(
    root: Path,
    changed_paths: Iterable[str | Path],
    *,
    profile: str = "default",
) -> list[VerificationCommand]:
    root = root.resolve()
    changed = [Path(item).resolve() for item in changed_paths if _inside(root, Path(item))]
    commands: list[VerificationCommand] = []

    python_files = [str(path) for path in changed if path.suffix == ".py" and path.exists()]
    if python_files:
        commands.append(VerificationCommand("python-syntax", (sys.executable, "-m", "py_compile", *python_files), 45.0))

    if (root / ".git").exists() and shutil.which("git"):
        # Windows tool writers may produce CRLF. Treat the carriage return at
        # end-of-line as a valid line ending while still checking other
        # whitespace errors in the patch.
        commands.append(
            VerificationCommand(
                "diff-check",
                ("git", "-c", "core.whitespace=cr-at-eol", "diff", "--check"),
                120.0,
            )
        )

    python_project = any((root / marker).exists() for marker in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py"))
    runtests = root / "tests" / "runtests.py"
    if profile in {"python", "python-swebench"} and runtests.is_file():
        # Django-style repositories ship their own runner and may not include
        # pytest in the task image.  Prefer the repository's supported runner
        # over guessing a third-party test command.
        labels: list[str] = []
        tests_root = root / "tests"
        for path in changed:
            if _inside(tests_root, path) and path.suffix == ".py":
                relative = path.relative_to(tests_root).with_suffix("")
                labels.append(".".join(relative.parts))
        argv = [sys.executable, "tests/runtests.py", "-v", "0"]
        if (root / "django").is_dir():
            argv.append("--settings=test_sqlite")
        # Never turn a source-only SWE-bench change into a full repository
        # test run.  The benchmark runner supplies the official focused test
        # command when one is available; without it, syntax and diff checks
        # remain useful and bounded evidence for the runtime gate.
        if labels or profile != "python-swebench":
            argv.extend(sorted(set(labels)))
            commands.append(VerificationCommand("project-tests", tuple(argv), 300.0, focused=True))
    elif python_project and (root / "tests").is_dir():
        changed_tests = [str(path.relative_to(root)) for path in changed if _inside(root / "tests", path) and path.suffix == ".py"]
        # Do not silently run an entire repository after a source-only edit.
        # Coding tasks must supply focused acceptance tests explicitly; a
        # static-only gate is reported as INCONCLUSIVE by the orchestrator.
        if changed_tests:
            argv = (sys.executable, "-m", "pytest", "-q", *changed_tests)
            commands.append(VerificationCommand("pytest", argv, 180.0, focused=True))
        elif profile == "python-swebench":
            # Preserve a stable evidence slot for source-only SWE-bench
            # changes without guessing a repository-wide test command.
            commands.append(VerificationCommand(
                "pytest", (sys.executable, "-c", "print('no focused tests available')"), 30.0, focused=False
            ))

    # SWE-bench Verified is a Python-only campaign.  Some repositories carry
    # incidental package metadata, but invoking npm there is both irrelevant
    # and unsafe when the host executable path is not present in the image.
    if profile in {"python", "python-swebench"}:
        return commands

    package_json = root / "package.json"
    npm = shutil.which("npm")
    if package_json.exists() and npm:
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        for script in ("lint", "typecheck", "test", "build"):
            if script in scripts:
                commands.append(VerificationCommand(f"npm-{script}", (npm, "run", script), 180.0, focused=True))

    if (root / "go.mod").exists() and shutil.which("go"):
        commands.append(VerificationCommand("go-test", ("go", "test", "./..."), 180.0, focused=True))
    if (root / "Cargo.toml").exists() and shutil.which("cargo"):
        commands.append(VerificationCommand("cargo-test", ("cargo", "test"), 240.0, focused=True))

    return commands
