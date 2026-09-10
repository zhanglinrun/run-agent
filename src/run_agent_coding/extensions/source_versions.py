"""Identify extension Python packages and load their source without stale bytecode."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import stat
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from pathlib import Path
from types import CodeType, ModuleType


class SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname: str) -> CodeType:
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


class _ExtensionSourceFinder(MetaPathFinder):
    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith("run_agent_extension_") or "." not in fullname:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = SourceOnlyLoader(fullname, spec.loader.path)
        return spec


def enable_extension_source_imports() -> None:
    if not any(isinstance(finder, _ExtensionSourceFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ExtensionSourceFinder())


def source_version(entry: Path, package_dir: Path | None) -> str:
    """Hash Python source and manifests; mutable experience assets live in resources."""
    root = package_dir or entry.parent
    paths = [entry]
    if package_dir is not None:
        paths = []
        for folder, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in {".git", "__pycache__"})
            if any(
                (Path(folder) / name).is_symlink() or (Path(folder) / name).is_junction()
                for name in directories
            ):
                raise ValueError("Extension package contains a directory link")
            paths.extend(
                Path(folder) / name
                for name in sorted(files)
                if Path(name).suffix in {".py", ".toml"}
            )
    digest = hashlib.sha256()
    total = 0
    for path in paths:
        size = path.stat()
        total += size.st_size
        if (
            path.is_symlink()
            or not stat.S_ISREG(size.st_mode)
            or len(paths) > 1024
            or total > 8 * 1024 * 1024
        ):
            raise ValueError("Extension source exceeds its file limit or contains a file link")
        content = path.read_bytes()
        after = path.stat()
        if (size.st_size, size.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Extension source changed during capture")
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()
