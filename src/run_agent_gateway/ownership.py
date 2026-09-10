"""Single-machine process lock, separate from the database fencing generation."""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from typing import BinaryIO


class GatewayProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("Gateway process lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                module = import_module("msvcrt")
                module.locking(handle.fileno(), module.LK_NBLCK, 1)
            else:
                module = import_module("fcntl")
                module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
