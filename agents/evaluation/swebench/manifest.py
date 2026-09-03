"""Stable evidence hashes for reproducible SWE-bench campaigns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from .adapter import sha256_file


def git_head(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True,
        text=True, encoding="utf-8", errors="replace", shell=False, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def tool_schema_hash() -> str | None:
    try:
        from ...tools import tool_definitions
        payload = json.dumps(tool_definitions, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:
        return None


def policy_hash(root: Path) -> str | None:
    try:
        policy_root = root.resolve() / "agents" / "policy"
        files = sorted(path for path in policy_root.glob("*.py") if path.is_file())
        payload = {path.name: sha256_file(path) for path in files}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except Exception:
        return None


def image_digest(image: str) -> str | None:
    if not image:
        return None
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or "").strip()
    return value or None


__all__ = ["git_head", "tool_schema_hash", "policy_hash", "image_digest"]
