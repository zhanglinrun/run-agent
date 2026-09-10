"""Build the distribution, install it into a clean environment, and validate it.

This is the release-readiness half of the gate: it proves the installed artifact
works outside the source tree. The clean environment is keyed by the wheel hash,
so an unchanged wheel reuses the previous installation instead of reinstalling.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def work_paths(root: Path = ROOT) -> tuple[Path, Path, Path]:
    """Return the (dist, env, report) locations used by the distribution steps.

    The report is written into the tracked evidence directory, matching the
    existing docs/implementation/*.xml convention, so the clean-installation
    result is durable evidence rather than throwaway build output.
    """
    work = root / ".run" / "verify"
    report = root / "docs" / "implementation" / "distribution-check.json"
    return work / "dist", work / "env", report


def bin_dir(env: Path) -> Path:
    """Return the Scripts (Windows) or bin directory of a virtual environment."""
    return env / ("Scripts" if os.name == "nt" else "bin")


def executable(env: Path, name: str) -> Path:
    """Return the path of a console script inside a virtual environment."""
    filename = f"{name}.exe" if os.name == "nt" else name
    return bin_dir(env) / filename


def run(argv: tuple[str, ...], cwd: Path) -> int:
    """Run a command with streamed output and return its exit code."""
    print("$ " + " ".join(argv), flush=True)
    done = subprocess.run(argv, cwd=cwd, text=True, encoding="utf-8", errors="replace")
    return done.returncode


def only_wheel(dist: Path) -> Path:
    """Return the single wheel built into dist, or fail loudly."""
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {dist}, found {len(wheels)}")
    return wheels[0]


def sha256_of(path: Path) -> str:
    """Return the hex sha256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def do_build(outdir: Path) -> int:
    """Build wheel and sdist into a freshly emptied output directory."""
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True)
    print(f"building wheel and sdist into {outdir}", flush=True)
    return run((sys.executable, "-m", "build", "--outdir", str(outdir)), ROOT)


def do_install(dist: Path, env: Path) -> int:
    """Install the wheel into a clean virtual environment, cached by wheel hash."""
    wheel = only_wheel(dist)
    digest = sha256_of(wheel)
    marker = env / ".wheel-sha256"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
        print(f"cache hit: {env.name} already holds {wheel.name} ({digest[:12]})", flush=True)
        return 0
    print(f"cache miss: installing {wheel.name} ({digest[:12]}) into a fresh environment")
    if env.exists():
        shutil.rmtree(env)
    if run((sys.executable, "-m", "venv", str(env)), ROOT) != 0:
        return 1
    argv = (
        str(executable(env, "python")),
        "-m",
        "pip",
        "install",
        "--quiet",
        "--disable-pip-version-check",
        str(wheel),
    )
    code = run(argv, ROOT)
    if code == 0:
        marker.write_text(digest + "\n", encoding="utf-8")
    return code


def do_validate(env: Path, output: Path) -> int:
    """Run the existing clean-installation validator against the built environment."""
    validator = ROOT / "scripts" / "validate_distribution.py"
    argv = (
        str(executable(env, "python")),
        str(validator),
        "--python",
        str(executable(env, "python")),
        "--run",
        str(executable(env, "run")),
        "--output",
        str(output),
    )
    return run(argv, ROOT)


def module_path() -> str:
    """Return this file's path, used to re-invoke it as a subprocess."""
    return str(Path(__file__).resolve())


def build_argv(root: Path = ROOT) -> tuple[str, ...]:
    """Command that builds the distribution."""
    return (sys.executable, module_path(), "build", "--outdir", str(work_paths(root)[0]))


def install_argv(root: Path = ROOT) -> tuple[str, ...]:
    """Command that installs the wheel into the cached clean environment."""
    dist, env, _ = work_paths(root)
    return (sys.executable, module_path(), "install", "--dist", str(dist), "--env", str(env))


def validate_argv(root: Path = ROOT) -> tuple[str, ...]:
    """Command that validates the clean installation."""
    _, env, report = work_paths(root)
    return (sys.executable, module_path(), "validate", "--env", str(env), "--output", str(report))


def pip_check_argv(root: Path = ROOT) -> tuple[str, ...]:
    """Command that runs the dependency check inside the clean environment."""
    _, env, _ = work_paths(root)
    return (str(executable(env, "python")), "-m", "pip", "check")


def main() -> int:
    """Dispatch the build / install / validate actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build")
    build.add_argument("--outdir", type=Path, required=True)
    install = actions.add_parser("install")
    install.add_argument("--dist", type=Path, required=True)
    install.add_argument("--env", type=Path, required=True)
    validate = actions.add_parser("validate")
    validate.add_argument("--env", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "build":
        return do_build(args.outdir)
    if args.action == "install":
        return do_install(args.dist, args.env)
    return do_validate(args.env, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
