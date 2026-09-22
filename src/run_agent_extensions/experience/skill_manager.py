"""Read-only Skill catalog and verifier-gated publication primitives."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .scopes import Scope
from .skill_guard import ScanResult, lint_content, parse_frontmatter, scan_skill, scan_text
from .skill_ledger import SkillLedger
from .skill_usage import SkillUsage

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None

SkillAction = Literal["list", "view", "propose"]
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION = 1024
MAX_BODY_CHARS = 100_000
MAX_FILE_BYTES = 1_048_576
EVOLUTION_OWNER = "evolution"


class SkillWriteError(ValueError):
    """A Skill operation refused by validation or publication policy."""


@dataclass(frozen=True, slots=True)
class SkillRoots:
    user: Path
    project: Path

    def directory(self, scope: Scope) -> Path:
        return self.user if scope == "user" else self.project


@dataclass(frozen=True, slots=True)
class SkillWriteResult:
    path: Path
    message: str
    lint: tuple[str, ...] = ()
    scan: str = ""
    ledger_id: str | None = None
    system_prompt_preview: str = ""
    change: dict[str, str] | None = None
    changed: bool = True


@dataclass(frozen=True, slots=True)
class SkillInfo:
    scope: Scope
    name: str
    description: str
    created_by: str
    pinned: bool
    category: str | None = None
    path: Path | None = None

    @property
    def managed(self) -> bool:
        """Compatibility name: only verifier-gated evolution may manage this Skill."""
        return self.created_by == EVOLUTION_OWNER

    @property
    def state(self) -> str:
        """Automatic stale/archive state no longer exists for active Skills."""
        return "active"

    @property
    def use_count(self) -> int:
        """Consultation telemetry is no longer collected."""
        return 0


class SkillManager:
    """Catalog reads plus the only supported path for publishing a candidate body."""

    def __init__(
        self,
        roots: SkillRoots,
        *,
        guard: bool = True,
        ledger: bool = True,
        session_id: str | None = None,
    ) -> None:
        self.roots = roots
        self.guard = guard
        self.session_id = session_id
        # The old sidecar is read for pinned flags only. Existing files are never deleted.
        self.usage: dict[Scope, SkillUsage] = {
            "user": SkillUsage(roots.user),
            "project": SkillUsage(roots.project),
        }
        self.ledger: dict[Scope, SkillLedger] = {
            "user": SkillLedger(roots.user, enabled=ledger),
            "project": SkillLedger(roots.project, enabled=ledger),
        }

    def names(self, scope: Scope) -> list[tuple[str, str]]:
        return [(info.name, info.description) for info in self.describe(scope)]

    def find(self, scope: Scope, name: str) -> Path | None:
        root = self.roots.directory(scope)
        if not root.is_dir() or not NAME_PATTERN.fullmatch(name):
            return None
        direct = root / name
        if not _path_has_redirect(direct, root) and (direct / "SKILL.md").is_file():
            return direct
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".") or not entry.is_dir() or _path_has_redirect(entry, root):
                continue
            nested = entry / name
            if (
                not _path_has_redirect(nested, root)
                and (nested / "SKILL.md").is_file()
                and not (entry / "SKILL.md").is_file()
            ):
                return nested
        return None

    def describe(self, scope: Scope) -> list[SkillInfo]:
        found: list[SkillInfo] = []
        for category, directory in self._iter_skill_dirs(scope):
            try:
                metadata, _ = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                continue
            found.append(
                SkillInfo(
                    scope=scope,
                    name=directory.name,
                    description=str(metadata.get("description") or ""),
                    created_by=str(metadata.get("created_by") or "user"),
                    pinned=self.usage[scope].is_pinned(directory.name),
                    category=category,
                    path=directory,
                )
            )
        return found

    def view(
        self,
        scope: Scope,
        name: str,
        file_path: str = "SKILL.md",
        *,
        count_usage: bool = False,
    ) -> str:
        del count_usage
        target = self._resolve(scope, name, file_path)
        if not target.is_file():
            raise SkillWriteError(f"{file_path} does not exist in skill {name!r}")
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillWriteError(f"could not read {file_path} in skill {name!r}") from exc

    def main_content(self, scope: Scope, name: str) -> str | None:
        directory = self.find(scope, name)
        if directory is None:
            return None
        try:
            return (directory / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillWriteError(f"could not read SKILL.md in skill {name!r}") from exc

    def digest(self, scope: Scope, name: str) -> str | None:
        content = self.main_content(scope, name)
        return hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None

    def is_pinned(self, scope: Scope, name: str) -> bool:
        return self.usage[scope].is_pinned(name)

    def is_evolution_owned(self, scope: Scope, name: str) -> bool:
        content = self.main_content(scope, name)
        if content is None:
            return False
        metadata, _ = parse_frontmatter(content)
        return str(metadata.get("created_by") or "") == EVOLUTION_OWNER

    def validate_candidate(self, name: str, content: str) -> tuple[tuple[str, ...], ScanResult]:
        _valid_name(name)
        _validate_skill_text(content, name)
        metadata, _ = parse_frontmatter(content)
        if str(metadata.get("created_by") or "") != EVOLUTION_OWNER:
            raise SkillWriteError(
                f"candidate frontmatter must declare created_by: {EVOLUTION_OWNER}"
            )
        result = scan_text(content, f"{name}/SKILL.md")
        if result.blocked:
            raise SkillWriteError(f"security scan refused the candidate:\n{result.report()}")
        return tuple(finding.format() for finding in lint_content(content)), result

    def adopt_evolution(self, scope: Scope, name: str) -> SkillWriteResult:
        """Explicitly transfer one existing Skill to verifier-gated evolution ownership."""
        with self.write_scope(scope):
            directory = self.find(scope, name)
            if directory is None:
                raise SkillWriteError(f"skill {name!r} not found in the {scope} scope")
            if self.is_pinned(scope, name):
                raise SkillWriteError(f"skill {name!r} is pinned and cannot be adopted")
            path = directory / "SKILL.md"
            previous = path.read_text(encoding="utf-8")
            updated = _set_created_by(previous, EVOLUTION_OWNER)
            self.validate_candidate(name, updated)
            if updated == previous:
                return SkillWriteResult(
                    path, f"{scope}/{name} is already evolution-owned", changed=False
                )
            before = self.ledger[scope].capture_before(directory)
            _atomic_write(path, updated)
            try:
                scan = self._scan_directory(directory)
            except SkillWriteError:
                _atomic_write(path, previous)
                raise
            ledger_id = self.ledger[scope].record(
                "adopt",
                name,
                actor="user",
                before=before,
                after_root=directory,
                evidence={"ownership": EVOLUTION_OWNER, "session_id": self.session_id},
            )
            return SkillWriteResult(
                path,
                f"{scope}/{name} is now evolution-owned",
                tuple(finding.format() for finding in lint_content(updated, skill_dir=directory)),
                scan.report(),
                ledger_id,
            )

    def publish_candidate(
        self,
        scope: Scope,
        name: str,
        content: str,
        *,
        expected_base_digest: str | None,
        candidate_id: str,
        candidate_digest: str,
        report_id: str,
        source_session: str,
        source_run: str,
        probes: list[dict[str, str]],
    ) -> SkillWriteResult:
        """Atomically replace one SKILL.md after rechecking ownership and base drift."""
        self.validate_candidate(name, content)
        actual_candidate = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if actual_candidate != candidate_digest:
            raise SkillWriteError("candidate content digest changed before publication")
        lock_scopes: tuple[Scope, ...] = (
            ("user", "project") if expected_base_digest is None else (scope,)
        )
        with self.write_scope(*lock_scopes):
            existing = self.find(scope, name)
            current_content = self.main_content(scope, name) if existing is not None else None
            current_digest = (
                hashlib.sha256(current_content.encode("utf-8")).hexdigest()
                if current_content is not None
                else None
            )
            if current_digest != expected_base_digest:
                raise SkillWriteError(
                    "base digest drifted; reject or supersede this candidate and propose again"
                )
            if existing is not None:
                if self.is_pinned(scope, name):
                    raise SkillWriteError(f"skill {name!r} is pinned")
                if not self.is_evolution_owned(scope, name):
                    raise SkillWriteError(
                        f"skill {name!r} is user-owned; run /evolve adopt {name} first"
                    )
                directory = existing
            else:
                for other_scope in ("user", "project"):
                    if other_scope != scope and self.find(other_scope, name) is not None:
                        raise SkillWriteError(
                            f"a skill named {name!r} already exists in the {other_scope} scope"
                        )
                directory = self.roots.directory(scope) / name
                if _path_has_redirect(directory, self.roots.directory(scope)):
                    raise SkillWriteError("Skill path contains a symlink or junction")
                directory.mkdir(parents=True, exist_ok=True)
            path = directory / "SKILL.md"
            previous = current_content
            before = self.ledger[scope].capture_before(directory if existing is not None else None)
            _atomic_write(path, content)
            try:
                scan = self._scan_directory(directory)
            except SkillWriteError:
                if previous is None:
                    shutil.rmtree(directory, ignore_errors=True)
                else:
                    _atomic_write(path, previous)
                raise
            ledger_id = self.ledger[scope].record(
                "publish",
                name,
                actor="evolution",
                before=before,
                after_root=directory,
                evidence={
                    "candidate_id": candidate_id,
                    "candidate_digest": candidate_digest,
                    "report_id": report_id,
                    "source_session": source_session,
                    "source_run": source_run,
                    "probes": probes,
                },
            )
            if ledger_id is None:
                if previous is None:
                    shutil.rmtree(directory, ignore_errors=True)
                else:
                    _atomic_write(path, previous)
                raise SkillWriteError("publication ledger failed; the Skill was restored")
            return SkillWriteResult(
                path,
                f"Published verified candidate {candidate_id} to {scope}/{name}.",
                tuple(finding.format() for finding in lint_content(content, skill_dir=directory)),
                scan.report(),
                ledger_id,
            )

    @contextmanager
    def write_scope(self, *scopes: Scope) -> Iterator[None]:
        handles: list[Any] = []
        try:
            for scope in sorted(set(scopes)):
                root = self.roots.directory(scope)
                root.mkdir(parents=True, exist_ok=True)
                handle = (root / ".write.lock").open("a+", encoding="ascii")
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
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                try:
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()

    def _iter_skill_dirs(self, scope: Scope) -> list[tuple[str | None, Path]]:
        root = self.roots.directory(scope)
        if not root.is_dir():
            return []
        found: list[tuple[str | None, Path]] = []
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".") or not entry.is_dir() or _path_has_redirect(entry, root):
                continue
            if (entry / "SKILL.md").is_file():
                found.append((None, entry))
                continue
            for nested in sorted(entry.iterdir(), key=lambda item: item.name):
                if (
                    nested.is_dir()
                    and not _path_has_redirect(nested, root)
                    and (nested / "SKILL.md").is_file()
                ):
                    found.append((entry.name, nested))
        return found

    def _resolve(self, scope: Scope, name: str, file_path: str) -> Path:
        _valid_name(name)
        directory = self.find(scope, name)
        if directory is None:
            raise SkillWriteError(f"skill {name!r} not found in the {scope} scope")
        relative = Path(file_path.replace("\\", "/"))
        if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
            raise SkillWriteError("file_path must be relative to the skill directory")
        if relative.name == "SKILL.md" and len(relative.parts) == 2 and relative.parts[0] == name:
            relative = Path("SKILL.md")
        target = directory / relative
        if _path_has_redirect(target, directory):
            raise SkillWriteError("Skill path contains a symlink or junction")
        try:
            target.resolve().relative_to(directory.resolve())
        except (OSError, ValueError) as exc:
            raise SkillWriteError("file_path escapes the skill directory") from exc
        return target

    def _scan_directory(self, directory: Path) -> ScanResult:
        if not self.guard:
            return ScanResult(directory.name, "safe")
        result = scan_skill(directory)
        if result.blocked:
            raise SkillWriteError(f"security scan refused the write:\n{result.report()}")
        return result


def _valid_name(name: str) -> str:
    if not name:
        raise SkillWriteError("Skill name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise SkillWriteError(f"Skill name exceeds {MAX_NAME_LENGTH} characters.")
    if not NAME_PATTERN.fullmatch(name):
        raise SkillWriteError(
            f"Invalid skill name {name!r}. Use lowercase letters, numbers, hyphens, dots and "
            "underscores; it must start with a letter or digit."
        )
    return name


def _validate_skill_text(text: str, name: str) -> None:
    if "\x00" in text:
        raise SkillWriteError("SKILL.md must be text")
    if len(text) > MAX_BODY_CHARS or len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise SkillWriteError("SKILL.md exceeds the size limit")
    metadata, body = parse_frontmatter(text)
    if not metadata:
        raise SkillWriteError("SKILL.md must start with YAML frontmatter (---)")
    if str(metadata.get("name") or "").strip() != name:
        raise SkillWriteError(f"frontmatter name must be {name!r}")
    description = str(metadata.get("description") or "").strip()
    if not description:
        raise SkillWriteError("Frontmatter must include 'description' field.")
    if len(description) > MAX_DESCRIPTION:
        raise SkillWriteError(f"Description exceeds {MAX_DESCRIPTION} characters.")
    if not body.strip():
        raise SkillWriteError("SKILL.md must have content after the frontmatter")


def _set_created_by(text: str, owner: str) -> str:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise SkillWriteError("SKILL.md must start with YAML frontmatter (---)")
    end = normalized.find("\n---", 4)
    if end < 0:
        raise SkillWriteError("SKILL.md frontmatter is not closed")
    header = normalized[4:end]
    lines = header.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.startswith("created_by:"):
            lines[index] = f"created_by: {owner}"
            replaced = True
            break
    if not replaced:
        lines.append(f"created_by: {owner}")
    return "---\n" + "\n".join(lines) + normalized[end:]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        else:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _is_path_redirect(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _path_has_redirect(path: Path, root: Path) -> bool:
    current = path.absolute()
    boundary = root.absolute()
    while True:
        if _is_path_redirect(current):
            return True
        if current == boundary or current.parent == current:
            return False
        current = current.parent


__all__ = [
    "EVOLUTION_OWNER",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION",
    "MAX_FILE_BYTES",
    "MAX_NAME_LENGTH",
    "NAME_PATTERN",
    "SkillAction",
    "SkillInfo",
    "SkillManager",
    "SkillRoots",
    "SkillWriteError",
    "SkillWriteResult",
]
