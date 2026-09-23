"""One read-mostly view over Skills and memories (the Curator's "journey").

Node identity is stable and addressable:

* a Skill is ``<scope>/<name>`` (for example ``project/deploy-runbook``);
* a memory is ``memory:<source>:<index>`` where ``source`` is ``memory`` for
  ``MEMORY.md`` or ``profile`` for ``USER.md``, and ``index`` counts the non-empty
  blocks of the file split on a bare ``§``.

``list`` and ``show`` are read-only. ``delete`` follows the project's ownership
split: deleting a Skill *archives* it (a whole-directory move into ``.archive``, never
a deletion), while deleting a memory is refused with a pointer to ``/memory``, because
memory writes belong to the memory extension. That refusal is deliberate and differs
from hermes, where ``journey delete`` rewrites the memory file itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from run_agent_coding.paths import RunAgentPaths

from .library import CuratorLibrary, LibraryMutation, SkillRecord
from .state import epoch_to_iso

MEMORY_SOURCES: dict[str, str] = {"memory": "MEMORY.md", "profile": "USER.md"}
MEMORY_DELIMITER = "\u00a7"
MEMORY_PREFIX = "memory:"
MEMORY_REFUSAL = (
    "memory content is owned by the memory extension; use `/memory` to change or "
    "delete it (the Curator never rewrites memory files)"
)
MAX_MEMORY_BODY_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One ``§``-delimited memory block."""

    id: str
    source: str
    index: int
    local_index: int
    path: Path
    title: str
    body: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "index": self.index,
            "local_index": self.local_index,
            "path": str(self.path),
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class JourneyNode:
    """One addressable node in the unified Skill + memory view."""

    id: str
    kind: str
    label: str
    detail: str
    timestamp: float | None = None


def memory_files(paths: RunAgentPaths, cwd: Path) -> tuple[tuple[str, Path], ...]:
    """Return the user-then-project file for each memory source.

    Only the two files the built-in memory provider owns are read: ``MEMORY.md`` and
    ``USER.md`` in the user scope root, plus their project-scope counterparts. A
    missing file simply contributes no blocks.
    """
    project = paths.project_run_agent_dir(cwd)
    files: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for source, filename in MEMORY_SOURCES.items():
        for directory in (paths.home, project):
            path = directory / filename
            if path in seen:
                continue
            seen.add(path)
            files.append((source, path))
    return tuple(files)


def memory_entries(paths: RunAgentPaths, cwd: Path) -> tuple[MemoryEntry, ...]:
    """Read every memory block as an addressable node.

    Empty blocks are skipped and do not consume an index, so ``memory:memory:1`` always
    names the second non-empty block of ``MEMORY.md``.
    """
    entries: list[MemoryEntry] = []
    index = 0
    for source, path in memory_files(paths, cwd):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        local = 0
        for block in text.split(MEMORY_DELIMITER):
            body = block.strip()
            if not body:
                continue
            title = next((line.strip().lstrip("#").strip() for line in body.splitlines()), "")
            entries.append(
                MemoryEntry(
                    id=f"{MEMORY_PREFIX}{source}:{index}",
                    source=source,
                    index=index,
                    local_index=local,
                    path=path,
                    title=title or f"{path.name} block {local}",
                    body=body[:MAX_MEMORY_BODY_CHARS],
                )
            )
            index += 1
            local += 1
    return tuple(entries)


def skill_nodes(records: tuple[SkillRecord, ...]) -> tuple[JourneyNode, ...]:
    """Return one node per Skill record."""
    return tuple(
        JourneyNode(
            id=record.key,
            kind="skill",
            label=record.name,
            detail=(
                f"{record.scope}  created_by={record.created_by}  state={record.state}"
                f"{'  pinned' if record.pinned else ''}"
            ),
            timestamp=record.anchor,
        )
        for record in records
    )


def memory_nodes(entries: tuple[MemoryEntry, ...]) -> tuple[JourneyNode, ...]:
    """Return one node per memory block."""
    return tuple(
        JourneyNode(
            id=entry.id,
            kind="memory",
            label=entry.title,
            detail=f"{entry.source} ({entry.path.name})",
            timestamp=None,
        )
        for entry in entries
    )


def journey_nodes(
    records: tuple[SkillRecord, ...],
    entries: tuple[MemoryEntry, ...],
) -> tuple[JourneyNode, ...]:
    """Return the unified, stable-ordered node list."""
    return (*skill_nodes(records), *memory_nodes(entries))


def render_list(nodes: tuple[JourneyNode, ...]) -> str:
    """Render ``journey list``: one line per node id."""
    if not nodes:
        return "Nothing to show yet: no Skills and no memory blocks."
    lines: list[str] = []
    for node in nodes:
        glyph = "\u25c6" if node.kind == "memory" else "\u25cf"
        stamp = epoch_to_iso(node.timestamp) or "-"
        lines.append(f"{node.id}  {glyph} {node.label}  {node.detail}  {stamp}")
    return "\n".join(lines)


def render_show(
    node_id: str,
    *,
    records: tuple[SkillRecord, ...],
    entries: tuple[MemoryEntry, ...],
    library: CuratorLibrary,
) -> str:
    """Render ``journey show <id>``; an unknown id is a refusal, not an empty view."""
    if is_memory_id(node_id):
        entry = next((item for item in entries if item.id == node_id), None)
        if entry is None:
            return f"Refused: no memory node with id {node_id!r}"
        return "\n".join(
            [
                f"id: {entry.id}",
                "kind: memory",
                f"source: {entry.source} ({entry.path.name})",
                f"path: {entry.path}",
                f"block: {entry.local_index}",
                f"title: {entry.title}",
                "",
                entry.body,
                "",
                f"note: {MEMORY_REFUSAL}",
            ]
        )
    record = next((item for item in records if item.key == node_id), None)
    if record is None:
        return f"Refused: no Skill node with id {node_id!r}"
    content = library.manager.main_content(record.scope, record.name) or "(SKILL.md unreadable)"
    return "\n".join(
        [
            f"id: {record.key}",
            "kind: skill",
            f"path: {record.path}",
            f"created_by: {record.created_by}",
            f"pinned: {'yes' if record.pinned else 'no'}",
            f"state: {record.state}",
            f"digest: {record.digest[:12]}",
            f"last_mutation: {epoch_to_iso(record.last_mutation_at) or 'never'}",
            f"last_consulted: {epoch_to_iso(record.last_consulted_at) or 'never'}",
            f"protected: {library.protected(record) or 'no'}",
            "",
            content,
        ]
    )


def delete_node(
    node_id: str,
    *,
    records: tuple[SkillRecord, ...],
    library: CuratorLibrary,
    now: datetime | None = None,
) -> LibraryMutation:
    """Delete one journey node: a Skill is archived, a memory is always refused."""
    if is_memory_id(node_id):
        return LibraryMutation(False, f"Refused: {MEMORY_REFUSAL}")
    record = next((item for item in records if item.key == node_id), None)
    if record is None:
        return LibraryMutation(False, f"Refused: no Skill node with id {node_id!r}")
    if record.pinned:
        return LibraryMutation(
            False, f"Refused: {record.key} is pinned and is never archived automatically"
        )
    return library.archive(record, reason="journey delete", now=now)


def is_memory_id(node_id: str) -> bool:
    """Whether an id addresses a memory block."""
    return node_id.startswith(MEMORY_PREFIX)


def parse_memory_id(node_id: str) -> tuple[str, int] | None:
    """Parse ``memory:<source>:<index>``, or ``None`` when it is malformed."""
    parts = node_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "memory" or parts[1] not in MEMORY_SOURCES:
        return None
    if not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


__all__ = [
    "MEMORY_DELIMITER",
    "MEMORY_PREFIX",
    "MEMORY_REFUSAL",
    "MEMORY_SOURCES",
    "JourneyNode",
    "MemoryEntry",
    "delete_node",
    "is_memory_id",
    "journey_nodes",
    "memory_entries",
    "memory_files",
    "memory_nodes",
    "parse_memory_id",
    "render_list",
    "render_show",
    "skill_nodes",
]
