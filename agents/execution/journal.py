"""Content-hash journal that observes all mutations, including Shell writes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable

from .workspace import git_changed_paths


@dataclass(frozen=True)
class ChangeSnapshot:
    path: str
    before_sha256: str | None
    after_sha256: str | None


def _digest(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkspaceJournal:
    def __init__(self, root: str | Path, *, baseline_dirty_paths: Iterable[str | Path] = ()) -> None:
        self.root = Path(root).resolve()
        self.baseline_dirty_paths = {str(Path(path).resolve()) for path in baseline_dirty_paths}
        self._before: dict[str, str | None] = {}
        self._after: dict[str, str | None] = {}

    def prime(self) -> None:
        """Record the initial content hash of the disposable workspace."""
        for path in self.root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                raw = str(path.resolve())
                self._before[raw] = _digest(path)
                self._after[raw] = self._before[raw]

    def reset(self) -> None:
        self._before.clear()
        self._after.clear()
        self.prime()

    def observe(self, paths: Iterable[str | Path] = ()) -> tuple[str, ...]:
        observed = {str(Path(path).resolve()) for path in paths}
        observed.update(git_changed_paths(self.root))
        observed.update(self.changed_paths)
        for raw in observed:
            path = Path(raw)
            self._before.setdefault(raw, None)
            self._after[raw] = _digest(path)
        return self.changed_paths

    def content_hashes(self) -> dict[str, str | None]:
        return {path: self._after.get(path) for path in self.changed_paths}

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(path for path, before in self._before.items() if before != self._after.get(path)))

    def snapshots(self) -> tuple[ChangeSnapshot, ...]:
        return tuple(ChangeSnapshot(path, self._before[path], self._after.get(path)) for path in self.changed_paths)

    def to_dict(self) -> dict[str, object]:
        return {"root": str(self.root), "changed_paths": list(self.changed_paths), "snapshots": [item.__dict__ for item in self.snapshots()]}
