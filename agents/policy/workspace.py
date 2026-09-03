"""Workspace path boundary used by policy and tool execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Iterable

from ..runtime.scope import current_workspace


class WorkspaceViolation(PermissionError):
    """Raised when a tool path escapes every configured workspace root."""


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True)
class WorkspaceBoundary:
    """Resolve paths and reject traversal outside explicitly allowed roots."""

    root: Path = field(default_factory=current_workspace)
    managed_roots: tuple[Path, ...] = ()

    def __init__(self, root: str | Path | None = None, managed_roots: Iterable[str | Path] = ()) -> None:
        object.__setattr__(self, "root", Path(root or current_workspace()).resolve())
        object.__setattr__(
            self,
            "managed_roots",
            tuple(Path(item).expanduser().resolve() for item in managed_roots),
        )

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.root, *self.managed_roots)

    def contains(self, path: str | Path) -> bool:
        candidate = _normalized(Path(path).expanduser().resolve())
        for root in self.roots:
            base = _normalized(root)
            try:
                if os.path.commonpath([candidate, base]) == base:
                    return True
            except ValueError:
                continue
        return False

    def resolve(self, raw_path: str | Path, *, must_exist: bool = False) -> Path:
        if str(raw_path).strip() == "":
            raise WorkspaceViolation("empty path is not allowed")
        path = Path(raw_path).expanduser()
        candidate = path if path.is_absolute() else self.root / path
        candidate = candidate.resolve(strict=False)
        if not self.contains(candidate):
            roots = ", ".join(str(item) for item in self.roots)
            raise WorkspaceViolation(f"path escapes allowed workspace roots: {candidate}; roots={roots}")
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate
