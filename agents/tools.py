"""Built-in coding tools + permission gate for Run Agent (C01)."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

ToolDef = dict[str, Any]

READ_TOOLS = {"read_file", "list_files", "grep"}
WRITE_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"bash"}
PLAN_TOOLS = {"enter_plan_mode", "exit_plan_mode"}

MAX_RESULT_CHARS = 50_000

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
]

# OpenAI / Anthropic 都认的「名字 + 描述 + JSON Schema」；agent 里再转成 OpenAI tools 格式
TOOL_DEFINITIONS: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read a file. Returns content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write/overwrite a file. Creates parent dirs if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace an exact old_string with new_string in a file. old_string must be unique.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files under a directory, optionally filtered by glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Base directory (default: .)"},
                "pattern": {
                    "type": "string",
                    "description": 'Optional glob, e.g. "**/*.py". If omitted, list direct children.',
                },
            },
            "required": [],
        },
    },
    {
        "name": "grep",
        "description": "Search for a regex pattern in files. Returns path:line:content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory or file (default: .)"},
                "include": {"type": "string", "description": 'File glob, e.g. "*.py"'},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command and return stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default 30000)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "enter_plan_mode",
        "description": (
            "Enter plan mode (read-only planning). In plan mode you may only read files "
            "and write to the designated plan file."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "exit_plan_mode",
        "description": (
            "Exit plan mode after writing your plan to the plan file. "
            "Triggers user approval before implementation."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def to_openai_tools(defs: list[ToolDef] | None = None) -> list[dict[str, Any]]:
    """Convert TOOL_DEFINITIONS to OpenAI Chat Completions tools= format."""
    out: list[dict[str, Any]] = []
    for t in defs or TOOL_DEFINITIONS:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in DANGEROUS_PATTERNS)


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return str(a).replace("\\", "/") == str(b).replace("\\", "/")


def check_permission(
    mode: str,
    name: str,
    inp: dict,
    plan_file_path: str | None = None,
) -> dict[str, str]:
    """Return {"action": "allow"|"deny"|"confirm", "message": ...}."""
    if mode == "bypassPermissions":
        return {"action": "allow", "message": ""}

    if name in PLAN_TOOLS:
        return {"action": "allow", "message": ""}

    if name in READ_TOOLS:
        return {"action": "allow", "message": ""}

    if mode == "plan":
        if name in WRITE_TOOLS:
            target = inp.get("file_path") or inp.get("path")
            if plan_file_path and _same_path(str(target) if target is not None else None, plan_file_path):
                return {"action": "allow", "message": ""}
            return {"action": "deny", "message": f"Blocked in plan mode: {name}"}
        if name in SHELL_TOOLS:
            return {"action": "deny", "message": f"Blocked in plan mode: {name}"}
        return {"action": "allow", "message": ""}

    if mode == "acceptEdits" and name in WRITE_TOOLS:
        return {"action": "allow", "message": ""}

    if name == "bash" and is_dangerous(inp.get("command", "")):
        msg = inp.get("command", "")
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk): {msg}"}
        return {"action": "confirm", "message": msg}

    return {"action": "allow", "message": ""}


def _resolve_path(raw: str, *, must_exist: bool = True) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    if must_exist and not path.exists():
        raise FileNotFoundError(raw)
    return path


def _truncate(result: str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep]
        + f"\n\n[... truncated {len(result) - keep * 2} chars ...]\n\n"
        + result[-keep:]
    )


def _read_file(inp: dict) -> str:
    try:
        path = _resolve_path(inp["file_path"])
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        return "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(lines))
    except Exception as e:
        return f"Error reading file: {e}"


def _write_file(inp: dict) -> str:
    try:
        path = _resolve_path(inp["file_path"], must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")
        n = len(inp["content"].split("\n"))
        return f"Successfully wrote to {inp['file_path']} ({n} lines)"
    except Exception as e:
        return f"Error writing file: {e}"


def _edit_file(inp: dict) -> str:
    try:
        path = _resolve_path(inp["file_path"])
        content = path.read_text(encoding="utf-8", errors="replace")
        old = inp["old_string"]
        new = inp["new_string"]
        if old not in content:
            return f"Error: old_string not found in {inp['file_path']}"
        count = content.count(old)
        if count > 1:
            return f"Error: old_string found {count} times; must be unique."
        path.write_text(content.replace(old, new, 1), encoding="utf-8")
        return f"Successfully edited {inp['file_path']}"
    except Exception as e:
        return f"Error editing file: {e}"


def _list_files(inp: dict) -> str:
    try:
        base = _resolve_path(inp.get("path") or ".", must_exist=True)
        pattern = inp.get("pattern")
        names: list[str] = []

        if pattern:
            for p in base.glob(pattern):
                if not p.is_file():
                    continue
                rel = str(p.relative_to(base))
                if ".git" in Path(rel).parts or ".venv" in Path(rel).parts:
                    continue
                names.append(rel)
                if len(names) >= 200:
                    break
        else:
            for p in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if p.name.startswith("."):
                    continue
                prefix = "[dir] " if p.is_dir() else "[file] "
                names.append(prefix + p.name)
                if len(names) >= 200:
                    break

        return "\n".join(names) if names else "No files found."
    except Exception as e:
        return f"Error listing files: {e}"


def _grep(inp: dict) -> str:
    pattern = inp["pattern"]
    include = inp.get("include")
    try:
        root_path = _resolve_path(inp.get("path") or ".", must_exist=True)
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    except Exception as e:
        return f"Error: {e}"

    matches: list[str] = []

    def search_file(file_path: Path) -> None:
        if len(matches) >= 200:
            return
        if include and not fnmatch.fnmatch(file_path.name, include):
            return
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for i, line in enumerate(text.split("\n"), 1):
            if regex.search(line):
                matches.append(f"{file_path}:{i}:{line}")
                if len(matches) >= 200:
                    return

    def walk(d: Path) -> None:
        if len(matches) >= 200:
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.name.startswith(".") or p.name in {"node_modules", ".venv"}:
                continue
            if p.is_dir():
                walk(p)
            elif p.is_file():
                search_file(p)

    if root_path.is_file():
        search_file(root_path)
    else:
        walk(root_path)

    if not matches:
        return "No matches found."
    out = "\n".join(matches[:100])
    if len(matches) > 100:
        out += f"\n... and {len(matches) - 100} more matches"
    return out


def _bash(inp: dict) -> str:
    try:
        timeout_ms = float(inp.get("timeout") or 30_000)
        timeout_s = timeout_ms / 1000.0
        result = subprocess.run(
            inp["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=str(Path.cwd()),
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            out = result.stdout.strip()
            parts = [f"Command failed (exit {result.returncode})"]
            if out:
                parts.append(f"Stdout:\n{out}")
            if err:
                parts.append(f"Stderr:\n{err}")
            return "\n".join(parts)
        return result.stdout or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    except Exception as e:
        return f"Error: {e}"


_HANDLERS = {
    "read_file": _read_file,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "list_files": _list_files,
    "grep": _grep,
    "bash": _bash,
}


async def execute_tool(name: str, inp: dict) -> str:
    """Run a tool by name. Permission must be checked by Agent before calling."""
    handler = _HANDLERS.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    try:
        return _truncate(handler(inp))
    except Exception as e:
        return f"Error executing {name}: {e}"
