"""Disposable workspace preparation for container execution."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


def prepare_workspace_for_container(workspace: str | Path) -> Path:
    """Make a disposable bind-mounted checkout writable by uid 1000."""
    raw_root = Path(workspace).expanduser()
    if raw_root.is_symlink():
        raise ValueError(f"sandbox workspace must not be a symlink: {raw_root}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    for path in [root, *root.rglob("*")]:
        try:
            if path.is_symlink():
                continue
            current_mode = path.stat().st_mode
            mode = 0o777 if path.is_dir() or current_mode & 0o111 else 0o666
            os.chmod(path, mode)
        except OSError:
            pass
    return root


def scrub_workspace_credentials(workspace: str | Path) -> list[str]:
    """Remove common credential files from a disposable task checkout."""
    raw_root = Path(workspace).expanduser()
    if raw_root.is_symlink():
        raise ValueError(f"sandbox workspace must not be a symlink: {raw_root}")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    sensitive_files = {
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "credentials.json",
    }
    sensitive_dirs = {".ssh", ".aws", ".azure"}
    removed: list[str] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        name = path.name.lower()
        is_env = name == ".env" or (
            name.startswith(".env.") and name != ".env.example"
        )
        if path.is_symlink() and (
            is_env or name in sensitive_files or name in sensitive_dirs
        ):
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(root)))
        elif path.is_file() and (is_env or name in sensitive_files):
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(root)))
        elif path.is_dir() and name in sensitive_dirs:
            shutil.rmtree(path)
            removed.append(str(path.relative_to(root)))
    return sorted(removed)
