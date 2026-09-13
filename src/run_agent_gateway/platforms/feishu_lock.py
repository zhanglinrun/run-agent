"""One gateway per Feishu app id on this machine.

Two WebSocket clients for the same app fight over the event stream and each answers
half of the messages. hermes refuses to start a second gateway on the same credential
with a machine-local scoped lock; this is the same idea with the process identity the
delivery ledger already uses, so a lock left behind by a dead process is reclaimed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from run_agent_coding.host.process_identity import process_identity

logger = logging.getLogger(__name__)
_LOCK_INITIALIZATION_GRACE_SECONDS = 30.0


def lock_path_for(locks_dir: Path, app_id: str) -> Path:
    digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:16]
    return locks_dir / f"feishu-{digest}.json"


class AppInstanceLock:
    """A JSON lock file naming the process that owns one app id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> tuple[bool, dict[str, Any] | None]:
        """Take the lock atomically; reclaim a lock left by a dead process."""
        if self._held:
            return True, None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self._write_exclusive()
                self._held = True
                return True, None
            except FileExistsError:
                existing = self._read()
                if existing is not None and not self._stale(existing):
                    return False, existing
                if existing == {} and not self._incomplete_is_stale():
                    return False, {"state": "initializing"}
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        return False, self._read() or {}

    def _incomplete_is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return age >= _LOCK_INITIALIZATION_GRACE_SECONDS

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        existing = self._read()
        if existing is None or existing.get("pid") != os.getpid():
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.debug("[feishu] could not remove app lock %s", self.path, exc_info=True)

    def _read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # An empty or half-written file is held briefly while its owner finishes.
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _stale(record: dict[str, Any]) -> bool:
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return True
        if pid == os.getpid():
            return False
        identity = record.get("identity")
        try:
            live = process_identity(pid)
        except (OSError, ValueError):
            return True
        if live is None:
            return True
        # Same pid but a different start time means the pid was recycled.
        return isinstance(identity, str) and identity != live

    def _write_exclusive(self) -> None:
        pid = os.getpid()
        try:
            identity = process_identity(pid)
        except (OSError, ValueError):
            identity = None
        record = {
            "pid": pid,
            "identity": identity,
            "argv": list(sys.argv),
            "acquired_at": time.time(),
        }
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(self.path, flags)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False))
        except BaseException:
            with contextlib.suppress(OSError):
                self.path.unlink()
            raise


__all__ = ["AppInstanceLock", "lock_path_for"]
