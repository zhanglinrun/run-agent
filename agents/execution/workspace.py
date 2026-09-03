"""Workspace state and deterministic Git helpers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


def _git(root: Path, *argv: str) -> str:
    try:
        result = subprocess.run(["git", *argv], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=30)
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def git_changed_paths(root: str | Path, *, baseline: set[str] | None = None) -> set[str]:
    root = Path(root).resolve()
    raw = _git(root, "status", "--porcelain", "-z")
    paths: set[str] = set()
    fields = [item for item in raw.split("\0") if item]
    index = 0
    while index < len(fields):
        item = fields[index]
        status = item[:2] if len(item) >= 3 else ""
        value = item[3:] if len(item) >= 3 else item
        paths.add(str((root / value).resolve()))
        if status and ("R" in status or "C" in status) and index + 1 < len(fields):
            index += 1
            paths.add(str((root / fields[index]).resolve()))
        index += 1
    if baseline:
        paths.difference_update(baseline)
    return paths


def git_diff(root: str | Path, *, include_untracked: bool = True) -> str:
    root = Path(root).resolve()
    tracked = _git(root, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-color")
    additions: list[str] = []
    if include_untracked:
        raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        paths = tuple(item for item in raw.split("\0") if item and not _is_ephemeral_patch_path(item))
        for value in paths:
            result = subprocess.run(
                ["git", "diff", "--no-index", "--binary", "--no-ext-diff", "--", os.devnull, value],
                cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                shell=False, timeout=30,
            )
            if result.returncode in {0, 1} and result.stdout:
                additions.append(result.stdout)
    return tracked + "".join(additions)


def _is_ephemeral_patch_path(value: str) -> bool:
    path = Path(value)
    ignored_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"}
    return bool(ignored_parts.intersection(path.parts)) or path.suffix in {".pyc", ".pyo"} or path.name in {".coverage"}


def git_base_commit(root: str | Path) -> str:
    return _git(Path(root).resolve(), "rev-parse", "HEAD").strip()
