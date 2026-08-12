"""
项目级长期记忆（C04）。

- 目录：~/.run/projects/<cwd-hash>/memory/
- 每条记忆：带 YAML frontmatter 的 Markdown
- MEMORY.md：自动索引，注入 system prompt
- 召回：扫头 → side query 选文件 → <system-reminder> 注入
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .frontmatter import format_frontmatter, parse_frontmatter

# side query: async (system, user) -> str
SideQueryFn = Callable[[str, str], Any]

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000
MAX_MEMORY_FILES = 200
MAX_MEMORY_BYTES_PER_FILE = 4096
MAX_SESSION_MEMORY_BYTES = 60 * 1024


class MemoryEntry:
    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(
        self,
        name: str,
        description: str,
        type: str,
        filename: str,
        content: str,
    ) -> None:
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content


def _project_hash() -> str:
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    d = Path.home() / ".run" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_index_path() -> Path:
    return get_memory_dir() / "MEMORY.md"


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower())
    s = s.strip("_")
    return (s[:40] if s else "memory")


def list_memories() -> list[MemoryEntry]:
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            meta = result.meta
            if not meta.get("name") or not meta.get("type"):
                continue
            t = meta["type"] if meta["type"] in VALID_TYPES else "project"
            entries.append(
                MemoryEntry(
                    name=meta["name"],
                    description=meta.get("description", ""),
                    type=t,
                    filename=f.name,
                    content=result.body,
                )
            )
        except Exception:
            continue
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
    return entries


def save_memory(name: str, description: str, type: str, content: str) -> str:
    d = get_memory_dir()
    t = type if type in VALID_TYPES else "project"
    filename = f"{t}_{_slugify(name)}.md"
    text = format_frontmatter(
        {"name": name, "description": description, "type": t},
        content,
    )
    (d / filename).write_text(text, encoding="utf-8")
    _update_memory_index()
    return filename


def delete_memory(filename: str) -> bool:
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    _update_memory_index()
    return True


def _update_memory_index() -> None:
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")
    _get_index_path().write_text("\n".join(lines), encoding="utf-8")


def load_memory_index() -> str:
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text(encoding="utf-8", errors="replace")
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = (
            "\n".join(lines[:MAX_INDEX_LINES])
            + "\n\n[... truncated, too many memory entries ...]"
        )
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content[:MAX_INDEX_BYTES] + "\n\n[... truncated, index too large ...]"
    return content


class MemoryHeader:
    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(
        self,
        filename: str,
        file_path: str,
        mtime_ms: float,
        description: str | None,
        type: str | None,
    ) -> None:
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type


def scan_memory_headers() -> list[MemoryHeader]:
    d = get_memory_dir()
    headers: list[MemoryHeader] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            raw = f.read_text(encoding="utf-8", errors="replace")
            first30 = "\n".join(raw.split("\n")[:30])
            meta = parse_frontmatter(first30).meta
            t = meta.get("type")
            headers.append(
                MemoryHeader(
                    filename=f.name,
                    file_path=str(f),
                    mtime_ms=stat.st_mtime * 1000,
                    description=meta.get("description"),
                    type=t if t in VALID_TYPES else None,
                )
            )
        except Exception:
            continue
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


def memory_age(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (
        f"This memory is {days} days old. Memories are point-in-time observations, "
        "not live state — claims about code behavior may be outdated. "
        "Verify against current code before asserting as fact."
    )


SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array."""


class RelevantMemory:
    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str) -> None:
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header

    @property
    def size(self) -> int:
        return len(self.content.encode())


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    headers = scan_memory_headers()
    if not headers:
        return []

    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    manifest = format_memory_manifest(candidates)
    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )
        match = re.search(r"\{[\s\S]*\}", text or "")
        if not match:
            return []

        parsed = json.loads(match.group(0))
        selected_filenames = set(parsed.get("selected_memories", []))
        selected = [h for h in candidates if h.filename in selected_filenames][:5]

        result: list[RelevantMemory] = []
        for h in selected:
            content = Path(h.file_path).read_text(encoding="utf-8", errors="replace")
            if len(content.encode()) > MAX_MEMORY_BYTES_PER_FILE:
                content = (
                    content[:MAX_MEMORY_BYTES_PER_FILE]
                    + "\n\n[... truncated, memory file too large ...]"
                )
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:"
                if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            result.append(
                RelevantMemory(
                    path=h.file_path,
                    content=content,
                    mtime_ms=h.mtime_ms,
                    header=header_text,
                )
            )
        return result
    except Exception as e:
        if "cancel" in str(e).lower():
            return []
        print(f"[memory] semantic recall failed: {e}")
        return []


class MemoryPrefetch:
    def __init__(self, task: asyncio.Task) -> None:
        self.task = task
        self.consumed = False

    @property
    def settled(self) -> bool:
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    if not re.search(r"\s", query.strip()):
        return None
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    d = get_memory_dir()
    try:
        has_memories = any(
            f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir()
        )
    except OSError:
        return None
    if not has_memories:
        return None

    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    parts = []
    for m in memories:
        parts.append(
            f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>"
        )
    return "\n\n".join(parts)


def build_memory_prompt_section() -> str:
    index = load_memory_index()
    memory_dir = str(get_memory_dir())
    index_block = (
        "\n## Current Memory Index\n" + index
        if index
        else "\n(No memories saved yet.)"
    )
    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Save Memories
Use the write_file tool to create a memory file with YAML frontmatter:

```markdown
---
name: memory name
description: one-line description
type: user|feedback|project|reference
---
Memory content here.
```

Save to: `{memory_dir}/`
Filename format: `{{type}}_{{slugified_name}}.md`

The MEMORY.md index is auto-updated when you write to the memory directory — do NOT update it manually.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Ephemeral task details

## When to Recall
When the user asks you to remember or recall, or when prior context seems relevant.
{index_block}"""
