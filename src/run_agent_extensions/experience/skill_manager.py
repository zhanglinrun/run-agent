"""Model-managed Skills: create, edit, patch, delete and support files.

Skills are procedural memory, the way hermes-agent's ``skill_manage`` treats them:
memory says who the user is and what is true about the project; a Skill says how to
do a class of task. A Skill written here is an ordinary Run Agent skill, a directory
with a ``SKILL.md`` under the user or project skills directory (optionally inside a
category sub-directory), so the normal loader picks it up and freezes it on the next
session or ``/reload``.

Around every write:

- Validation of the name, the category, the frontmatter (a description that fits the
  prompt index budget on create), the body size and support-file paths.
- A security scan of the resulting directory when the guard is on; a dangerous
  verdict rolls the write back, a caution is reported.
- Provenance and ownership: a Skill the review fork writes is marked curator-managed in
  the usage sidecar; autonomous maintenance (the review fork, the curator) refuses
  pinned Skills and Skills the user owns, and must have viewed a file in the same
  review before it may rewrite it. A review delete archives instead of removing, and
  must name the umbrella that absorbed the content. A pinned Skill cannot be deleted
  by anyone through the tool; patches still go through.
- An audit ledger entry with before/after manifests and evidence, so any mutation can
  be rolled back one edit at a time.
- Usage telemetry: views, uses and patches are counted so the curator can reason
  about staleness and reuse-after-patch.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from run_agent_coding.host.learning import (
    LearnerOwnedAsset,
    is_agent_created,
    require_writeback,
    write_origin,
)

from .memory import MemoryScope
from .skill_guard import (
    SKILL_PROMPT_DESC_LIMIT,
    ScanResult,
    lint_content,
    parse_frontmatter,
    scan_skill,
    scan_text,
)
from .skill_ledger import SkillLedger
from .skill_usage import SkillUsage

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:  # pragma: no cover - exercised on POSIX hosts
    import fcntl

    msvcrt = None


SkillAction = Literal[
    "create", "edit", "patch", "delete", "write_file", "remove_file", "view", "list"
]

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SUPPORT_DIRS = ("references", "templates", "scripts", "assets")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION = 1024
MAX_BODY_CHARS = 100_000
MAX_FILE_BYTES = 1_048_576
CREATED_BY_REVIEW = "review"
CREATED_BY_AGENT = "agent"
SKILL_VERSION = "0.1.0"
SKILL_AUTHOR = "Run Agent"
SKILL_LICENSE = "MIT"


class SkillWriteError(ValueError):
    """A Skill write that was refused, with the reason a model can act on."""


@dataclass(frozen=True, slots=True)
class SkillRoots:
    user: Path
    project: Path

    def directory(self, scope: MemoryScope) -> Path:
        return self.user if scope == "user" else self.project


@dataclass(frozen=True, slots=True)
class SkillWriteResult:
    path: Path
    message: str
    lint: tuple[str, ...] = ()
    scan: str = ""
    ledger_id: str | None = None
    # Set when the description will be truncated in the prompt index (hermes'
    # ``system_prompt_preview``): what the model will actually see.
    system_prompt_preview: str = ""
    # For verbose notifications: what changed, in a few words.
    change: dict[str, str] = field(default_factory=dict)
    changed: bool = True


@dataclass(frozen=True, slots=True)
class SkillInfo:
    scope: MemoryScope
    name: str
    description: str
    created_by: str
    managed: bool
    pinned: bool
    state: str
    use_count: int
    category: str | None = None
    path: Path | None = None


@dataclass(slots=True)
class ReviewReadMarks:
    """Files the active review fork has loaded; a write needs its target here."""

    paths: set[str] = field(default_factory=set)

    def mark(self, path: Path) -> None:
        self.paths.add(str(path.resolve()))

    def has(self, path: Path) -> bool:
        return str(path.resolve()) in self.paths

    def reset(self) -> None:
        self.paths.clear()


class SkillManager:
    """Filesystem operations on the two skill scopes, with every guard applied."""

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
        self.usage: dict[MemoryScope, SkillUsage] = {
            "user": SkillUsage(roots.user),
            "project": SkillUsage(roots.project),
        }
        self.ledger: dict[MemoryScope, SkillLedger] = {
            "user": SkillLedger(roots.user, enabled=ledger),
            "project": SkillLedger(roots.project, enabled=ledger),
        }
        self.read_marks = ReviewReadMarks()
        # The curator sets this for the duration of its pass so the ledger names it.
        self.actor_override: str | None = None

    # -- reads ----------------------------------------------------------------------

    def names(self, scope: MemoryScope) -> list[tuple[str, str]]:
        return [(info.name, info.description) for info in self.describe(scope)]

    def find(self, scope: MemoryScope, name: str) -> Path | None:
        """The directory of a skill by name, at the root or inside one category."""
        root = self.roots.directory(scope)
        if not root.is_dir() or not NAME_PATTERN.fullmatch(name):
            return None
        direct = root / name
        if _path_has_redirect(direct, root):
            return None
        if (direct / "SKILL.md").is_file():
            return direct
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".") or not entry.is_dir() or _path_has_redirect(entry, root):
                continue
            nested = entry / name
            if _path_has_redirect(nested, root):
                continue
            if (nested / "SKILL.md").is_file() and not (entry / "SKILL.md").is_file():
                return nested
        return None

    def _iter_skill_dirs(self, scope: MemoryScope) -> list[tuple[str | None, Path]]:
        """Every (category, directory) holding a SKILL.md, root skills first."""
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

    def describe(self, scope: MemoryScope) -> list[SkillInfo]:
        usage = self.usage[scope]
        records = usage.load()
        found: list[SkillInfo] = []
        for category, directory in self._iter_skill_dirs(scope):
            try:
                metadata, _ = parse_frontmatter(
                    (directory / "SKILL.md").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                continue
            record = records.get(directory.name) or {}
            found.append(
                SkillInfo(
                    scope=scope,
                    name=directory.name,
                    description=str(metadata.get("description", "") or ""),
                    created_by=str(metadata.get("created_by") or "user"),
                    managed=record.get("created_by") == CREATED_BY_AGENT,
                    pinned=bool(record.get("pinned")),
                    state=str(record.get("state") or "active"),
                    use_count=int(record.get("use_count") or 0),
                    category=category,
                    path=directory,
                )
            )
        return found

    @contextmanager
    def write_scope(self, *scopes: MemoryScope) -> Iterator[None]:
        """Serialize content, usage and ledger writes for these Skill roots."""
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

    def view(
        self,
        scope: MemoryScope,
        name: str,
        file_path: str = "SKILL.md",
        *,
        count_usage: bool = True,
    ) -> str:
        target = self._resolve(scope, name, file_path)
        if not target.is_file():
            raise SkillWriteError(f"{file_path} does not exist in skill {name!r}")
        text = target.read_text(encoding="utf-8")
        if is_agent_created():
            self.read_marks.mark(target)
        if count_usage and target.name == "SKILL.md" and target.parent.name == name:
            self.usage[scope].bump_view(name)
        return text

    def record_use(self, scope: MemoryScope, name: str) -> None:
        self.usage[scope].bump_use(name)

    # -- writes ---------------------------------------------------------------------

    def create(
        self,
        scope: MemoryScope,
        name: str,
        description: str,
        body: str,
        *,
        category: str | None = None,
    ) -> SkillWriteResult:
        require_writeback()
        _valid_name(name)
        chosen_category = _valid_category(category)
        scopes: tuple[MemoryScope, ...] = ("user", "project")
        for other in scopes:
            existing = self.find(other, name)
            if existing is not None:
                raise SkillWriteError(
                    f"A skill named {name!r} already exists at {existing}; use edit or patch."
                )
        root = self.roots.directory(scope)
        directory = root / chosen_category / name if chosen_category else root / name
        if _path_has_redirect(directory, root):
            raise SkillWriteError("Skill path contains a symlink or junction")
        text = self._compose(name, description, body, new_skill=True)
        self._refuse_dangerous_text(text, f"{name}/SKILL.md")
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / "SKILL.md", text)
        try:
            scan = self._scan_or_rollback(directory, created=True)
        except SkillWriteError:
            self._prune_empty_category(directory.parent, root)
            raise
        agent_created = is_agent_created()
        self.usage[scope].record_created(name, agent_created=agent_created)
        ledger_id = self.ledger[scope].record(
            "create",
            name,
            actor=self._actor(),
            before=[],
            after_root=directory,
            evidence=self._evidence(category=chosen_category),
        )
        lint = tuple(f.format() for f in lint_content(text, skill_dir=directory))
        message = f"Created skill {name!r}. It loads on the next session or /reload."
        if chosen_category:
            message = (
                f"Created skill {name!r} in category {chosen_category!r}. It loads on the "
                "next session or /reload."
            )
        return SkillWriteResult(
            directory / "SKILL.md",
            message,
            lint,
            scan.report(),
            ledger_id,
            _prompt_preview(description),
            {"description": " ".join(description.split())[:120]},
        )

    def edit(
        self, scope: MemoryScope, name: str, description: str | None, body: str
    ) -> SkillWriteResult:
        path = self._writable(scope, name, "edit", path_hint="SKILL.md")
        previous = path.read_text(encoding="utf-8")
        metadata, _ = parse_frontmatter(previous)
        supplied, supplied_body = parse_frontmatter(body.strip())
        chosen = (
            description
            if description is not None
            else str(supplied.get("description", metadata.get("description", "")) or "")
        )
        created_by = metadata.get("created_by")
        text = self._compose(
            name,
            chosen,
            supplied_body if supplied else body,
            created_by=str(created_by) if created_by else None,
        )
        text = _preserve_frontmatter(previous, body.strip() if supplied else "", text)
        _validate_skill_text(text, name)
        self._refuse_dangerous_text(text, f"{name}/SKILL.md")
        if previous == text:
            return SkillWriteResult(
                path, f"Skill {name!r} is unchanged; no write needed.", changed=False
            )
        before = self.ledger[scope].capture_before(path.parent)
        _atomic_write(path, text)
        try:
            scan = self._scan_or_rollback(path.parent, created=False)
        except SkillWriteError:
            _atomic_write(path, previous)
            raise
        self.usage[scope].bump_patch(name)
        ledger_id = self.ledger[scope].record(
            "edit",
            name,
            actor=self._actor(),
            before=before,
            after_root=path.parent,
            evidence=self._evidence(file_path="SKILL.md"),
        )
        lint = tuple(f.format() for f in lint_content(text, skill_dir=path.parent))
        return SkillWriteResult(
            path,
            f"Skill {name!r} updated (full rewrite).",
            lint,
            scan.report(),
            ledger_id,
            _prompt_preview(chosen),
            {"description": " ".join(chosen.split())[:120]},
        )

    def patch(
        self,
        scope: MemoryScope,
        name: str,
        file_path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
    ) -> SkillWriteResult:
        skill_file = self._writable(scope, name, "patch", path_hint=file_path)
        target = self._resolve(scope, name, file_path)
        if not target.is_file():
            raise SkillWriteError(f"{file_path} does not exist in skill {name!r}")
        if not old:
            raise SkillWriteError("old_text cannot be empty")
        if old == new:
            raise SkillWriteError("new_text must differ from old_text")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            preview = text[:500] + ("..." if len(text) > 500 else "")
            raise SkillWriteError(f"old_text not found in {file_path}. File preview:\n{preview}")
        if occurrences > 1 and not replace_all:
            raise SkillWriteError(
                f"old_text matches {occurrences} places in {file_path}; include more "
                "surrounding context or pass replace_all=true"
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        if target == skill_file:
            _validate_skill_text(updated, name)
        else:
            _check_size(updated, file_path)
        self._refuse_dangerous_text(updated, f"{name}/{file_path}")
        before = self.ledger[scope].capture_before(skill_file.parent)
        _atomic_write(target, updated)
        try:
            scan = self._scan_or_rollback(skill_file.parent, created=False)
        except SkillWriteError:
            _atomic_write(target, text)
            raise
        self.usage[scope].bump_patch(name)
        count = occurrences if replace_all else 1
        ledger_id = self.ledger[scope].record(
            "patch",
            name,
            actor=self._actor(),
            before=before,
            after_root=skill_file.parent,
            evidence=self._evidence(file_path=file_path, replacements=count),
        )
        lint: tuple[str, ...] = ()
        if target == skill_file:
            lint = tuple(f.format() for f in lint_content(updated, skill_dir=skill_file.parent))
        label = "SKILL.md" if target == skill_file else file_path
        return SkillWriteResult(
            target,
            f"Patched {label} in skill {name!r} ({count} replacement{'s' if count > 1 else ''}).",
            lint,
            scan.report(),
            ledger_id,
            "",
            {"old": _clip(old), "new": _clip(new)},
        )

    def write_file(
        self, scope: MemoryScope, name: str, file_path: str, content: str
    ) -> SkillWriteResult:
        skill_file = self._writable(
            scope, name, "write_file", path_hint=file_path, read_check=False
        )
        target = self._resolve(scope, name, file_path)
        _validate_support_path(target, skill_file.parent, file_path)
        _check_size(content, file_path)
        self._refuse_dangerous_text(content, f"{name}/{file_path}")
        existed = target.is_file()
        if existed and is_agent_created() and not self.read_marks.has(target):
            raise SkillWriteError(
                f"the review has not loaded {file_path} of skill {name!r} in this pass; "
                "view it first, then write using the content just returned"
            )
        previous = target.read_text(encoding="utf-8") if existed else None
        if previous == content:
            return SkillWriteResult(
                target, f"File {file_path!r} is unchanged; no write needed.", changed=False
            )
        before = self.ledger[scope].capture_before(skill_file.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)
        try:
            scan = self._scan_or_rollback(skill_file.parent, created=False)
        except SkillWriteError:
            if previous is not None:
                _atomic_write(target, previous)
            else:
                target.unlink(missing_ok=True)
            raise
        self.usage[scope].bump_patch(name)
        ledger_id = self.ledger[scope].record(
            "write_file",
            name,
            actor=self._actor(),
            before=before,
            after_root=skill_file.parent,
            evidence=self._evidence(file_path=file_path),
        )
        return SkillWriteResult(
            target,
            f"File {file_path!r} written to skill {name!r}.",
            (),
            scan.report(),
            ledger_id,
            "",
            {"file": file_path},
        )

    def remove_file(self, scope: MemoryScope, name: str, file_path: str) -> SkillWriteResult:
        skill_file = self._writable(scope, name, "remove_file", path_hint=file_path)
        target = self._resolve(scope, name, file_path)
        if target.name == "SKILL.md":
            raise SkillWriteError("use delete to remove the whole skill")
        _validate_support_path(target, skill_file.parent, file_path)
        if not target.is_file():
            available = [
                str(p.relative_to(skill_file.parent)).replace("\\", "/")
                for sub in SUPPORT_DIRS
                if (skill_file.parent / sub).is_dir()
                for p in (skill_file.parent / sub).rglob("*")
                if p.is_file()
            ]
            hint = f" Available: {', '.join(available)}" if available else ""
            raise SkillWriteError(f"{file_path} does not exist in skill {name!r}.{hint}")
        before = self.ledger[scope].capture_before(skill_file.parent)
        target.unlink()
        parent = target.parent
        if parent != skill_file.parent and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
        self.usage[scope].bump_patch(name)
        ledger_id = self.ledger[scope].record(
            "remove_file",
            name,
            actor=self._actor(),
            before=before,
            after_root=skill_file.parent,
            evidence=self._evidence(file_path=file_path),
        )
        return SkillWriteResult(
            target,
            f"File {file_path!r} removed from skill {name!r}.",
            (),
            "",
            ledger_id,
            "",
            {"file": file_path},
        )

    def delete(
        self, scope: MemoryScope, name: str, *, absorbed_into: str | None = None
    ) -> SkillWriteResult:
        """Delete a skill.

        ``absorbed_into`` declares intent: a named umbrella (which must exist) means the
        content was consolidated; an empty string means a deliberate prune; omitting it
        is accepted for the foreground but logged, because the curator's classification
        cannot tell consolidation from pruning without it. The review fork archives
        instead of deleting and may only do so with a named umbrella. A pinned skill is
        never deleted through the tool, whoever asks.
        """
        skill_file = self._writable(scope, name, "delete", path_hint="SKILL.md", read_check=False)
        directory = skill_file.parent
        root = self.roots.directory(scope)
        _validate_delete_target(directory, root)
        target = (absorbed_into or "").strip()
        if target:
            if target == name:
                raise SkillWriteError("absorbed_into cannot be the skill being deleted")
            if self.find(scope, target) is None:
                raise SkillWriteError(
                    f"absorbed_into={target!r} does not exist; create or patch the umbrella "
                    "first, then retry the delete"
                )
        if is_agent_created():
            if not target:
                raise SkillWriteError(
                    "the review may only archive a skill it has absorbed into an umbrella; "
                    "pass absorbed_into=<umbrella>. Staleness pruning is the curator's job."
                )
            before = self.ledger[scope].capture_before(directory)
            ok, message = self.usage[scope].archive(name)
            if not ok:
                raise SkillWriteError(message)
            archived = self.usage[scope].archive_dir / name
            ledger_id = self.ledger[scope].record(
                "archive",
                name,
                actor=self._actor(),
                before=before,
                after_root=archived if archived.is_dir() else None,
                evidence=self._evidence(absorbed_into=target, archived=True),
            )
            return SkillWriteResult(
                directory,
                f"Archived skill {name!r} ({message}); absorbed into {target!r}.",
                (),
                "",
                ledger_id,
            )
        if self.usage[scope].is_pinned(name):
            raise SkillWriteError(
                f"Skill {name!r} is pinned and cannot be deleted by skill_manage. Ask the user "
                f"to run `/curator unpin {name}` if they want to delete it. Patches and edits "
                "are allowed on pinned skills; only deletion is blocked."
            )
        if absorbed_into is None:
            logger.warning(
                "skill %r deleted without absorbed_into; consolidation vs prune is unknown", name
            )
        before = self.ledger[scope].capture_before(directory)
        shutil.rmtree(directory)
        self._prune_empty_category(directory.parent, root)
        self.usage[scope].forget(name)
        ledger_id = self.ledger[scope].record(
            "delete",
            name,
            actor=self._actor(),
            before=before,
            after_root=None,
            evidence=self._evidence(absorbed_into=absorbed_into, archived=False),
        )
        message = f"Deleted skill {name!r}."
        if target:
            message += f" Content absorbed into {target!r}."
        return SkillWriteResult(directory, message, (), "", ledger_id)

    # -- helpers --------------------------------------------------------------------

    def _dir(self, scope: MemoryScope, name: str) -> Path:
        _valid_name(name)
        found = self.find(scope, name)
        if found is not None:
            return found
        candidate = self.roots.directory(scope) / name
        if _path_has_redirect(candidate, self.roots.directory(scope)):
            raise SkillWriteError("Skill path contains a symlink or junction")
        return candidate

    def _resolve(self, scope: MemoryScope, name: str, file_path: str) -> Path:
        directory = self._dir(scope, name)
        relative = Path(file_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SkillWriteError("file_path must be relative to the skill directory")
        # ``<name>/SKILL.md`` is accepted as a spelling of the main file.
        if relative.name == "SKILL.md" and len(relative.parts) == 2 and relative.parts[0] == name:
            relative = Path("SKILL.md")
        target = directory / relative
        if _path_has_redirect(target, directory):
            raise SkillWriteError("Skill path contains a symlink or junction")
        return target

    def _writable(
        self,
        scope: MemoryScope,
        name: str,
        action: str,
        *,
        path_hint: str,
        read_check: bool = True,
    ) -> Path:
        """The SKILL.md of an existing Skill this writer is allowed to change."""
        require_writeback()
        path = self._dir(scope, name) / "SKILL.md"
        if not path.is_file():
            raise SkillWriteError(
                f"Skill {name!r} not found in the {scope} scope. Use skill_manage list to see "
                "available skills."
            )
        if is_agent_created():
            usage = self.usage[scope]
            if usage.is_pinned(name):
                raise LearnerOwnedAsset(
                    f"Refusing background {action} for pinned skill {name!r}: pinned skills are "
                    "off-limits to autonomous maintenance. Ask the user to run "
                    f"`/curator unpin {name}` if they want it changed."
                )
            if not usage.is_curator_managed(name):
                record = usage.load().get(name)
                detail = (
                    f"created_by={record.get('created_by')!r}"
                    if isinstance(record, dict)
                    else "no usage record"
                )
                raise LearnerOwnedAsset(
                    f"Refusing background {action} for skill {name!r}: the skill is not "
                    f"curator-managed ({detail}). user-owned skills are off-limits to "
                    f"autonomous curation. Run `/curator adopt {name}` to opt it in."
                )
            if read_check:
                target = self._resolve(scope, name, path_hint)
                if target.is_file() and not self.read_marks.has(target):
                    raise SkillWriteError(
                        f"the review has not loaded {path_hint} of skill {name!r} in this "
                        "pass; view it first, then write using the content just returned"
                    )
        return path

    def _compose(
        self,
        name: str,
        description: str,
        body: str,
        *,
        created_by: str | None = None,
        new_skill: bool = False,
    ) -> str:
        description = " ".join(description.split())
        if not description:
            raise SkillWriteError("a skill needs a one-line description")
        if len(description) > MAX_DESCRIPTION:
            raise SkillWriteError(f"Description exceeds {MAX_DESCRIPTION} characters.")
        if new_skill and len(description) > SKILL_PROMPT_DESC_LIMIT:
            raise SkillWriteError(
                f"Description is {len(description)} chars — new skills must fit the "
                f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, trigger "
                f"first, ends with a period). The skill index truncates longer descriptions to "
                f"{SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', destroying the routing signal. "
                "Move detail into the skill body."
            )
        if ":" in description or '"' in description:
            raise SkillWriteError("description cannot contain ':' or double quotes")
        body = body.strip()
        if not body:
            raise SkillWriteError("a skill needs a body")
        origin = created_by or (
            CREATED_BY_REVIEW if write_origin() == "background_review" else CREATED_BY_AGENT
        )
        text = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"version: {SKILL_VERSION}\n"
            f"author: {SKILL_AUTHOR}\n"
            f"license: {SKILL_LICENSE}\n"
            f"created_by: {origin}\n"
            "metadata:\n"
            "  run_agent:\n"
            "    tags: []\n"
            f"    created_by: {origin}\n"
            "---\n\n"
            f"{body}\n"
        )
        _validate_skill_text(text, name)
        return text

    def _refuse_dangerous_text(self, text: str, label: str) -> None:
        if not self.guard:
            return
        result = scan_text(text, label)
        if result.blocked:
            raise SkillWriteError(f"security scan refused the write:\n{result.report()}")

    def _scan_or_rollback(self, directory: Path, *, created: bool) -> ScanResult:
        if not self.guard:
            return ScanResult(directory.name, "safe")
        result = scan_skill(directory)
        if result.blocked:
            if created:
                shutil.rmtree(directory, ignore_errors=True)
            raise SkillWriteError(f"security scan refused the write:\n{result.report()}")
        return result

    def _actor(self) -> str:
        if self.actor_override:
            return self.actor_override
        return "review" if is_agent_created() else "agent"

    def _evidence(self, **fields: Any) -> dict[str, Any]:
        evidence = {key: value for key, value in fields.items() if value is not None}
        if self.session_id:
            evidence["session_id"] = self.session_id
        return evidence

    @staticmethod
    def _prune_empty_category(parent: Path, root: Path) -> None:
        """Drop an emptied category directory, never the root."""
        try:
            if parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    """Publish a single complete file, keeping executable support scripts executable."""
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


def _preserve_frontmatter(previous: str, supplied: str, composed: str) -> str:
    """Keep raw field blocks; explicit input replaces whole top-level fields.

    The caller's validated description wins. Identity and provenance stay fixed;
    ownership authorization remains in the usage sidecar and _writable guard.
    This preserves nested/custom YAML without extending the best-effort parser.
    """

    def blocks(text: str) -> dict[str, str]:
        normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.startswith("---\n"):
            return {}
        end = normalized.find("\n---", 4)
        if end == -1:
            return {}
        raw = normalized[4:end] + "\n"
        starts = list(re.finditer(r"(?m)^([^\s:#][^:\n]*):", raw))
        return {
            match.group(1).strip(): raw[
                match.start() if index else 0 : starts[index + 1].start()
                if index + 1 < len(starts)
                else len(raw)
            ]
            for index, match in enumerate(starts)
        }

    original = blocks(previous)
    merged = original.copy()
    merged.update(blocks(supplied))
    defaults = blocks(composed)
    for key in ("name", "created_by"):
        merged[key] = original.get(key, defaults[key])
    old_metadata, _ = parse_frontmatter(previous)
    new_metadata, _ = parse_frontmatter(composed)
    merged["description"] = (
        original["description"]
        if "description" in original
        and old_metadata.get("description") == new_metadata.get("description")
        else defaults["description"]
    )
    _, body = parse_frontmatter(composed)
    return "---\n" + "".join(merged.values()) + "---\n" + body


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


def _valid_category(category: str | None) -> str | None:
    if category is None:
        return None
    text = category.strip()
    if not text:
        return None
    if "/" in text or "\\" in text or not NAME_PATTERN.fullmatch(text):
        raise SkillWriteError(
            f"Invalid category {text!r}. Use lowercase letters, numbers, hyphens, dots and "
            "underscores; a category is a single directory name."
        )
    return text


def _check_size(text: str, label: str) -> None:
    if "\x00" in text:
        raise SkillWriteError(f"{label} must be text")
    size = len(text.encode())
    if size > MAX_FILE_BYTES:
        raise SkillWriteError(
            f"{label} is {size:,} bytes (limit: {MAX_FILE_BYTES:,} bytes / 1 MiB). Consider "
            "splitting into smaller files."
        )
    if len(text) > MAX_BODY_CHARS:
        raise SkillWriteError(
            f"{label} content is {len(text):,} characters (limit: {MAX_BODY_CHARS:,}). Consider "
            "splitting into a smaller file with supporting files."
        )


def _validate_skill_text(text: str, name: str) -> None:
    if len(text) > MAX_BODY_CHARS:
        raise SkillWriteError(
            f"SKILL.md content is {len(text):,} characters (limit: {MAX_BODY_CHARS:,}). Consider "
            "splitting into a smaller SKILL.md with supporting files in references/ or "
            "templates/."
        )
    metadata, body = parse_frontmatter(text)
    if not metadata:
        raise SkillWriteError(
            "SKILL.md must start with YAML frontmatter (---). See existing skills for format."
        )
    if str(metadata.get("name", "")).strip() != name:
        raise SkillWriteError(f"frontmatter name must be {name!r}")
    if not str(metadata.get("description", "") or "").strip():
        raise SkillWriteError("Frontmatter must include 'description' field.")
    if not body.strip():
        raise SkillWriteError(
            "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."
        )


def _validate_support_path(target: Path, skill_dir: Path, file_path: str) -> None:
    """Support files live under one of the allowed sub-directories, never elsewhere."""
    if target.name == "SKILL.md":
        raise SkillWriteError("use edit or patch for SKILL.md")
    relative = Path(file_path.replace("\\", "/"))
    if not relative.parts or relative.parts[0] not in SUPPORT_DIRS:
        raise SkillWriteError(
            f"File must be under one of: {', '.join(SUPPORT_DIRS)}. Got: {file_path!r}"
        )
    if len(relative.parts) < 2:
        raise SkillWriteError(
            f"Provide a file path, not just a directory. Example: '{relative.parts[0]}/myfile.md'"
        )
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise SkillWriteError(f"Path escapes the skill directory: {exc}") from exc


def _is_path_redirect(path: Path) -> bool:
    """A symlink or (on Windows) a directory junction: rmtree would follow it."""
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _path_has_redirect(path: Path, root: Path) -> bool:
    """Reject symlinks and junctions in a Skill path, including category parents."""
    current = path.absolute()
    boundary = root.absolute()
    while True:
        if _is_path_redirect(current):
            return True
        if current == boundary or current.parent == current:
            return False
        current = current.parent


def _validate_delete_target(directory: Path, root: Path) -> None:
    """Never recursively delete a redirect, the skills root, or anything outside it."""
    if _is_path_redirect(directory):
        raise SkillWriteError(
            f"Refusing to delete {directory}: the skill directory is a symlink/junction. "
            "Remove the link target manually if intended."
        )
    try:
        resolved = directory.resolve()
    except OSError as exc:
        raise SkillWriteError(
            f"Refusing to delete {directory}: could not resolve path ({exc})."
        ) from exc
    root_resolved = root.resolve()
    if resolved == root_resolved:
        raise SkillWriteError(
            f"Refusing to delete {directory}: resolves to the skills root itself, which would "
            "remove every installed skill."
        )
    if not resolved.is_relative_to(root_resolved):
        raise SkillWriteError(
            f"Refusing to delete {directory}: path does not resolve inside the skills root."
        )


def _prompt_preview(description: str) -> str:
    text = " ".join(description.split()).strip("'\"")
    if len(text) <= SKILL_PROMPT_DESC_LIMIT:
        return ""
    shown = text[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return (
        f'System prompt will show: "{shown}" — keep the trigger self-contained in the first '
        f"{SKILL_PROMPT_DESC_LIMIT - 3} chars."
    )


def _clip(text: str, limit: int = 200) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


__all__ = [
    "CREATED_BY_AGENT",
    "CREATED_BY_REVIEW",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION",
    "MAX_FILE_BYTES",
    "MAX_NAME_LENGTH",
    "SUPPORT_DIRS",
    "ReviewReadMarks",
    "SkillAction",
    "SkillInfo",
    "SkillManager",
    "SkillRoots",
    "SkillWriteError",
    "SkillWriteResult",
]
