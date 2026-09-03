"""Shared typed file operations for local and bind-mounted workspaces."""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path
import re

from ..policy.workspace import WorkspaceBoundary


class WorkspaceFileOperations:
    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._boundary = WorkspaceBoundary(self.workspace_root)

    def _resolve(self, raw: str | Path, *, must_exist: bool = False) -> Path:
        return self._boundary.resolve(raw, must_exist=must_exist)

    async def read_file(self, file_path: str) -> str:
        def read() -> str:
            path = self._resolve(file_path, must_exist=True)
            lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
            return "\n".join(
                f"{index + 1:4d} | {line}" for index, line in enumerate(lines)
            )

        try:
            return await asyncio.to_thread(read)
        except Exception as exc:
            return f"Error reading file: {exc}"

    async def write_file(self, file_path: str, content: str) -> str:
        def write() -> str:
            path = self._resolve(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
            return (
                f"Successfully wrote to {file_path} "
                f"({len(content.splitlines())} lines)"
            )

        try:
            return await asyncio.to_thread(write)
        except Exception as exc:
            return f"Error writing file: {exc}"

    async def edit_file(
        self, file_path: str, old_string: str, new_string: str
    ) -> str:
        def edit() -> str:
            path = self._resolve(file_path, must_exist=True)
            content = path.read_text(encoding="utf-8", errors="replace")
            count = content.count(old_string)
            if count == 0:
                return f"Error: old_string not found in {file_path}"
            if count > 1:
                return (
                    f"Error: old_string found {count} times in {file_path}. "
                    "Must be unique."
                )
            path.write_text(
                content.replace(old_string, new_string, 1),
                encoding="utf-8",
                newline="",
            )
            return f"Successfully edited {file_path}"

        try:
            return await asyncio.to_thread(edit)
        except Exception as exc:
            return f"Error editing file: {exc}"

    async def list_files(self, pattern: str, path: str = ".") -> str:
        def list_matches() -> list[str]:
            base = self._resolve(path)
            matches: list[str] = []
            for item in base.glob(pattern):
                if not item.is_file() or ".git" in item.parts:
                    continue
                try:
                    resolved = self._resolve(item, must_exist=True)
                except (OSError, ValueError):
                    continue
                matches.append(str(resolved.relative_to(self.workspace_root)))
            return matches

        try:
            files = await asyncio.to_thread(list_matches)
            return (
                "\n".join(files[:200])
                if files
                else "No files found matching the pattern."
            )
        except Exception as exc:
            return f"Error listing files: {exc}"

    async def search(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str:
        def search_files() -> str:
            base = self._resolve(path)
            regex = re.compile(pattern)
            hits: list[str] = []
            files = [base] if base.is_file() else base.rglob("*")
            for item in files:
                if (
                    not item.is_file()
                    or ".git" in item.parts
                    or (include and not fnmatch.fnmatch(item.name, include))
                ):
                    continue
                try:
                    resolved = self._resolve(item, must_exist=True)
                    lines = resolved.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except (OSError, ValueError):
                    continue
                display_path = resolved.relative_to(self.workspace_root)
                for number, line in enumerate(lines, 1):
                    if regex.search(line):
                        hits.append(f"{display_path}:{number}:{line}")
                        if len(hits) >= 100:
                            return "\n".join(hits)
            return "\n".join(hits) if hits else "No matches found."

        try:
            return await asyncio.to_thread(search_files)
        except Exception as exc:
            return f"Error searching files: {exc}"
