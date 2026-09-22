"""Compatibility reader for legacy Skill usage sidecars.

The verifier-gated extension no longer records consultations or drives lifecycle state.
Existing ``.usage.json`` and ``.archive/`` data are left untouched. Only the legacy
``pinned`` flag remains policy input for evolution publication.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

USAGE_FILE = ".usage.json"
ARCHIVE_DIR = ".archive"

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None


class SkillUsage:
    """Read legacy ownership data without reactivating its maintenance loop."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self.path = skills_dir / USAGE_FILE
        self.archive_dir = skills_dir / ARCHIVE_DIR

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(name): record for name, record in data.items() if isinstance(record, dict)}

    def get(self, name: str) -> dict[str, Any]:
        record = self.load().get(name)
        return dict(record) if isinstance(record, dict) else {}

    def is_pinned(self, name: str) -> bool:
        return bool(self.get(name).get("pinned"))

    def set_pinned(self, name: str, pinned: bool) -> None:
        """Compatibility helper for migrations/tests; no command automatically changes it."""
        if not name:
            return
        with self._locked():
            data = self.load()
            record = dict(data.get(name) or {})
            record["pinned"] = bool(pinned)
            data[name] = record
            self._save(data)

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".usage_", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="ascii")
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                handle.seek(0)
                if handle.read(1) == "":
                    handle.seek(0)
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()


__all__ = ["ARCHIVE_DIR", "USAGE_FILE", "SkillUsage"]
