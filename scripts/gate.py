"""Run the canonical gate with the project's own interpreter.

mise tasks call this so the checks always execute in the repository's virtual
environment, whatever ``python`` mise happens to resolve on PATH. This module
imports only the standard library, so any Python can launch it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def venv_python(root: Path) -> Path | None:
    """Return the project virtualenv interpreter, or None when it is absent."""
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    """Run scripts/verify.py in the virtualenv, forwarding every argument."""
    interpreter = venv_python(ROOT)
    if interpreter is None:
        print("no .venv found; create the project virtualenv before running the gate")
        return 1
    argv = (str(interpreter), str(ROOT / "scripts" / "verify.py"), *sys.argv[1:])
    return subprocess.run(argv, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
