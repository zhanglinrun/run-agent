"""Editable Markdown checkouts with immutable, explicit version baselines."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from run_agent_coding.host.contracts import ResourceVersion, ScopedServices

from .models import AssetKind, asset_key

FILENAMES = {"user": "USER.md", "memory": "MEMORY.md", "skill": "SKILL.md"}


def projection_path(
    root: Path, scoped: ScopedServices, kind: AssetKind, name: str, version: str
) -> Path:
    import re

    if re.fullmatch(r"[a-f0-9]{64}", version) is None:
        raise ValueError("Invalid resource version")
    key = asset_key(kind, name)
    identity = [scoped.projection_key, key, version]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    base = root / "experience"
    path = base / digest[:32] / FILENAMES[kind]
    if not path.resolve().is_relative_to(base.resolve()) or path.is_symlink():
        raise ValueError("Experience working copy leaves its scope")
    return path


async def checkout(
    root: Path, scoped: ScopedServices, kind: AssetKind, name: str, value: ResourceVersion
) -> Path:
    path = projection_path(root, scoped, kind, name, value.version)

    def create() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = json.dumps([scoped.projection_key, value.key, value.version])
        manifest = path.parent / "identity.json"
        try:
            with manifest.open("x", encoding="utf-8") as stream:
                stream.write(identity)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if manifest.read_text(encoding="utf-8") != identity:
                raise ValueError("Working-copy identity mismatch or interrupted checkout") from None
        try:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(value.content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            # Never overwrite a checkout, including user edits or partial crash output.
            if not path.is_file() or path.is_symlink():
                raise ValueError("Invalid experience working copy") from None

    await asyncio.to_thread(create)
    return path


async def verify_identity(path: Path, scoped: ScopedServices, key: str, version: str) -> None:
    expected = [scoped.projection_key, key, version]
    content = await asyncio.to_thread((path.parent / "identity.json").read_text, encoding="utf-8")
    if json.loads(content) != expected:
        raise ValueError("Working-copy identity differs from the requested asset and base")


async def read_working_copy(path: Path) -> str:
    def read() -> str:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 64000:
            raise ValueError("Working copy is missing, linked or exceeds 64 KiB")
        content = path.read_text(encoding="utf-8")
        if len(content.encode()) > 64000:
            raise ValueError("Working copy exceeds 64 KiB")
        return content

    return await asyncio.to_thread(read)
