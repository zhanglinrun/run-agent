"""Repository identity helpers shared by frozen evaluation evidence."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


def repository_evidence(root: Path | None = None) -> dict[str, Any]:
    """Bind evidence to both Git identity and the current package source bytes."""
    repository_root = (root or Path(__file__).resolve().parents[2]).resolve()
    revision = _git_output(repository_root, "rev-parse", "HEAD")
    status = _git_output(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    source_paths = _source_paths(repository_root)
    digest = hashlib.sha256()
    for path in source_paths:
        digest.update(path.relative_to(repository_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    status_text = status or ""
    return {
        "revision": revision,
        "dirty": bool(status_text.strip()),
        "status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        "source_sha256": digest.hexdigest(),
        "source_file_count": len(source_paths),
    }


def _source_paths(root: Path) -> tuple[Path, ...]:
    paths = [
        path
        for path in (root / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    paths.extend(
        path for name in ("pyproject.toml", ".python-version") if (path := root / name).is_file()
    )
    return tuple(sorted(paths, key=lambda path: path.relative_to(root).as_posix()))


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


__all__ = ["repository_evidence"]
