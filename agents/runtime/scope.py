"""Task-local project scope shared by workspace-aware feature modules."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator


_CURRENT_WORKSPACE: ContextVar[Path | None] = ContextVar("run_agent_workspace", default=None)


def current_workspace(default: str | Path | None = None) -> Path:
    """Return the task workspace without relying on the process working directory."""

    value = _CURRENT_WORKSPACE.get()
    if value is not None:
        return value
    return Path(default or Path.cwd()).expanduser().resolve()


@contextmanager
def bind_workspace(workspace: str | Path) -> Iterator[Path]:
    """Bind one workspace for the current async/task context."""

    resolved = Path(workspace).expanduser().resolve()
    token = _CURRENT_WORKSPACE.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT_WORKSPACE.reset(token)


__all__ = ["bind_workspace", "current_workspace"]
