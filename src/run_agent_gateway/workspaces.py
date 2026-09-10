"""Pinned Git worktrees and bounded result artifacts for background coding runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from run_agent_coding.host.contracts import ArtifactRef
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.sessions import canonical_json
from run_agent_coding.storage.settle import settle


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitRevision:
    repository: str
    common_directory: str
    commit: str
    relative_cwd: str


class BackgroundWorkspaces:
    def __init__(self, root: Path, artifacts: ArtifactStore) -> None:
        self.root = root.resolve()
        self.artifacts = artifacts
        self._lock = asyncio.Lock()

    @staticmethod
    def _git(path: Path, *arguments: str, limit: int = 32 * 1024 * 1024) -> bytes:
        # Regular files bound pipe memory and avoid waiting on inherited pipe handles.
        # Shells, hooks, external diff/textconv and optional index writes are disabled.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=",
                        "-c",
                        "submodule.recurse=false",
                        "-C",
                        str(path),
                        *arguments,
                    ],
                    stdout=output,
                    stderr=error,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    timeout=30,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise WorkspaceError(f"Git command failed: {type(exc).__name__}") from exc
            error.seek(0)
            if result.returncode:
                raise WorkspaceError(error.read(4096).decode(errors="replace").strip())
            if output.tell() > limit:
                raise WorkspaceError(f"Git output exceeds {limit} bytes")
            output.seek(0)
            return output.read()

    async def capture(self, cwd: Path) -> GitRevision:
        def capture() -> GitRevision:
            try:
                directory = cwd.resolve(strict=True)
            except OSError as exc:
                raise WorkspaceError("Background source directory is unavailable") from exc
            repository = Path(self._git(directory, "rev-parse", "--show-toplevel").decode().strip())
            if self._git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
                raise WorkspaceError("Background coding requires a clean Git workspace")
            commit = (
                self._git(repository, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
            )
            common = self._git(
                repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            tree = self._git(repository, "ls-tree", "-rz", "--full-tree", commit)
            # A gitlink is not a captured dependency. Linked files can reach back into
            # the main workspace, so neither is accepted by this first worktree policy.
            if any(row.startswith((b"160000 ", b"120000 ")) for row in tree.split(b"\0")):
                raise WorkspaceError("Background snapshots do not support submodules or symlinks")
            if self._git(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
                raise WorkspaceError("Workspace changed while capturing its revision")
            if self._git(repository, "rev-parse", "HEAD").decode().strip() != commit:
                raise WorkspaceError("Git HEAD changed while capturing its revision")
            return GitRevision(
                str(repository.resolve()),
                common.decode().strip(),
                commit,
                directory.relative_to(repository.resolve()).as_posix(),
            )

        result, cancelled = await settle(asyncio.to_thread(capture))
        if cancelled:
            raise asyncio.CancelledError
        return result

    def directory(self, task_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
            raise WorkspaceError("Invalid background task identity")
        path = self.root / task_id
        if not path.resolve().is_relative_to(self.root) or path.is_symlink() or path.is_junction():
            raise WorkspaceError("Background directory leaves its managed root")
        return path

    def workspace(self, task_id: str, revision: GitRevision) -> Path:
        relative = PurePosixPath(revision.relative_cwd)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in revision.relative_cwd
            or ":" in revision.relative_cwd
        ):
            raise WorkspaceError("Invalid background working directory")
        root = self.directory(task_id)
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root):
            raise WorkspaceError("Background working directory leaves its managed worktree")
        return path

    def _verify(self, task_id: str, revision: GitRevision, *, initial: bool = True) -> Path:
        destination = self.directory(task_id)
        repository = Path(revision.repository)
        rows = self._git(repository, "worktree", "list", "--porcelain", "-z").split(b"\0")
        registered = any(
            row.startswith(b"worktree ")
            and os.path.normcase(str(Path(os.fsdecode(row[9:])).resolve()))
            == os.path.normcase(str(destination))
            for row in rows
        )
        if not registered or not (destination / ".git").is_file():
            raise WorkspaceError("Background directory is not the registered Git worktree")
        actual_common = Path(
            self._git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        ).resolve()
        if actual_common != Path(revision.common_directory).resolve():
            raise WorkspaceError("Worktree belongs to another Git repository")
        if (
            initial
            and self._git(destination, "rev-parse", "HEAD").decode().strip() != revision.commit
        ):
            raise WorkspaceError("Worktree HEAD no longer matches the pinned commit")
        if not initial:
            self._git(destination, "merge-base", "--is-ancestor", revision.commit, "HEAD")
        return destination

    async def materialize(self, task_id: str, revision: GitRevision) -> Path:
        def materialize() -> Path:
            destination = self.directory(task_id)
            if destination.exists():
                self._verify(task_id, revision)
                if self._git(
                    destination, "status", "--porcelain=v1", "-z", "--untracked-files=all"
                ):
                    raise WorkspaceError("An existing background worktree has unreviewed changes")
            else:
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision.commit):
                    raise WorkspaceError("Background revision must be a full Git object ID")
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._git(
                    Path(revision.repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(destination),
                    revision.commit,
                )
                self._verify(task_id, revision)
            cwd = self.workspace(task_id, revision)
            if not cwd.is_dir():
                raise WorkspaceError("Pinned working directory is missing from the Git commit")
            return cwd

        async with self._lock:
            result, cancelled = await settle(asyncio.to_thread(materialize))
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def collect(self, task_id: str, revision: GitRevision) -> dict[str, Any]:
        def collect() -> dict[str, Any]:
            destination = self._verify(task_id, revision, initial=False)
            result_commit = self._git(destination, "rev-parse", "HEAD").decode().strip()
            before = self._git(
                destination, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            )
            patch = self._git(
                destination,
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                revision.commit,
                "--",
            )
            names = self._git(destination, "ls-files", "--others", "--exclude-standard", "-z")
            untracked = [os.fsdecode(name) for name in names.split(b"\0") if name]
            if len(untracked) > 1024:
                raise WorkspaceError("Background output exceeds 1024 untracked files")
            files = []
            captured: list[tuple[Path, ArtifactRef]] = []
            total = len(patch)
            for name in untracked:
                path = destination / name
                if not path.resolve().is_relative_to(destination) or path.is_symlink():
                    raise WorkspaceError("Background output contains a file outside its worktree")
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise WorkspaceError("Background output contains a non-regular file")
                total += info.st_size
                if total > 32 * 1024 * 1024:
                    raise WorkspaceError("Background output exceeds 32 MiB")
                body = path.read_bytes()
                if (path.stat().st_mtime_ns, len(body)) != (info.st_mtime_ns, info.st_size):
                    raise WorkspaceError("Background output changed during artifact capture")
                ref = self.artifacts.put_sync(body)
                captured.append((path, ref))
                files.append(
                    {
                        "path": name,
                        "artifact": asdict(ref),
                        "executable": bool(info.st_mode & 0o111),
                    }
                )
            after = self._git(
                destination, "status", "--porcelain=v1", "-z", "--untracked-files=all"
            )
            if (
                self._git(destination, "rev-parse", "HEAD").decode().strip() != result_commit
                or before != after
                or patch
                != self._git(
                    destination,
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    revision.commit,
                    "--",
                )
            ):
                raise WorkspaceError("Background output changed during artifact capture")
            for path, ref in captured:
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.digest:
                    raise WorkspaceError("Untracked output changed during artifact capture")
            report = {
                "revision": asdict(revision),
                "result_commit": result_commit,
                "workspace": str(destination),
                "patch": asdict(self.artifacts.put_sync(patch)),
                "untracked": files,
                "status_sha256": hashlib.sha256(before).hexdigest(),
            }
            manifest = self.artifacts.put_sync(canonical_json(report).encode())
            return {**report, "manifest": asdict(manifest)}

        result, cancelled = await settle(asyncio.to_thread(collect))
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def discard_clean(self, task_id: str, revision: GitRevision) -> None:
        """Only a verified clean worktree can be removed automatically."""

        def discard() -> None:
            destination = self._verify(task_id, revision)
            if self._git(
                destination,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ):
                raise WorkspaceError("Background worktree still contains output")
            self._git(Path(revision.repository), "worktree", "remove", str(destination))

        async with self._lock:
            _, cancelled = await settle(asyncio.to_thread(discard))
            if cancelled:
                raise asyncio.CancelledError


def decode_revision(value: str) -> GitRevision:
    return GitRevision(**json.loads(value))
