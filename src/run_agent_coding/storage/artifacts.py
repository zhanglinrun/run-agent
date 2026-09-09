"""Durable content-addressed files, written before committing references."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from run_agent_coding.host.contracts import ArtifactRef


class ArtifactCorrupt(RuntimeError):
    """A required immutable artifact is missing or fails its content hash."""


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def path(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid artifact SHA-256 digest")
        path = self.root / digest[:2] / digest
        if not path.resolve().is_relative_to(self.root):
            raise ArtifactCorrupt("Artifact path leaves its store")
        return path

    async def put(self, content: bytes) -> ArtifactRef:
        return await asyncio.to_thread(self.put_sync, content)

    def put_sync(self, content: bytes) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        destination = self.path(digest)
        ref = ArtifactRef(digest, len(content))
        if destination.exists():
            self.read_sync(ref)
            return ref
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            if os.name != "nt":
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()
        return ref

    async def read(self, ref: ArtifactRef) -> bytes:
        return await asyncio.to_thread(self.read_sync, ref)

    def read_sync(self, ref: ArtifactRef) -> bytes:
        try:
            content = self.path(ref.digest).read_bytes()
        except OSError as exc:
            raise ArtifactCorrupt(f"Required artifact is unavailable: {ref.digest}") from exc
        if len(content) != ref.size or hashlib.sha256(content).hexdigest() != ref.digest:
            raise ArtifactCorrupt(f"Artifact hash or size mismatch: {ref.digest}")
        return content
