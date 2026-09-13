"""Built-in Session extensions shipped with Run Agent.

Mirrors Pi's ``src/extensions`` package: optional behaviour that is still part of
the distribution, next to the extension runtime in ``run_agent_coding.extensions``
rather than in a loose repository directory. Each subpackage is an ordinary
directory extension exporting ``setup(api)``; hosts load one by name or by path
through ``--extension``. CodingApplication loads all built-ins by default for new
sessions; saved resource snapshots retain their prior set until explicitly refreshed.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BUILTIN_EXTENSIONS", "builtin_extension_names", "resolve_extension_path"]

_ROOT = Path(__file__).resolve().parent

BUILTIN_EXTENSIONS: dict[str, Path] = {
    "experience": _ROOT / "experience",
    "mcp": _ROOT / "mcp",
    "permission_policy": _ROOT / "permission_policy",
    "plan_mode": _ROOT / "plan_mode",
}


def builtin_extension_names() -> tuple[str, ...]:
    """The names ``--extension <name>`` accepts, in a stable order."""
    return tuple(sorted(BUILTIN_EXTENSIONS))


def resolve_extension_path(value: str | Path) -> Path:
    """Turn a built-in extension name into its package directory; leave paths alone.

    A name wins only when nothing exists at that relative path, so a directory the
    user actually has called ``mcp`` still loads as their own extension.
    """
    path = Path(value)
    if not path.exists() and len(path.parts) == 1 and path.name in BUILTIN_EXTENSIONS:
        return BUILTIN_EXTENSIONS[path.name]
    return path
