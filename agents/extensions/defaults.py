"""Default extension profile used by CLI, SDK, and benchmark tasks."""

from __future__ import annotations

from typing import Callable

from .context import setup_context, setup_memory
from .contracts import ExtensionSpec, SourceInfo
from .policy import setup_mcp, setup_permissions, setup_plan
from .quality import setup_acceptance, setup_correction, setup_verification
from .skills import setup_skill_evolution, setup_skills
from .subagents import setup_subagents
from .workspace import setup_execution, setup_workspace_tools


_DEFAULTS: tuple[tuple[str, Callable, tuple[str, ...]], ...] = (
    ("execution", setup_execution, ()),
    ("workspace-tools", setup_workspace_tools, ("execution",)),
    ("permissions", setup_permissions, ("workspace-tools",)),
    ("plan", setup_plan, ("permissions",)),
    ("context", setup_context, ("workspace-tools",)),
    ("memory", setup_memory, ("permissions",)),
    ("subagents", setup_subagents, ("permissions",)),
    ("skills", setup_skills, ("subagents", "permissions")),
    ("skill-evolution", setup_skill_evolution, ("skills",)),
    ("mcp", setup_mcp, ("permissions",)),
    ("verification", setup_verification, ("execution",)),
    ("correction", setup_correction, ("verification",)),
    ("acceptance", setup_acceptance, ("execution",)),
)


def default_extension_specs(
    disabled: frozenset[str] | set[str] = frozenset(),
) -> tuple[ExtensionSpec, ...]:
    """Return a dependency-closed default profile.

    Disabling a required capability also disables its dependents, preventing a
    partially configured extension graph from silently running.
    """

    omitted = set(disabled)
    changed = True
    while changed:
        changed = False
        for name, _setup, requires in _DEFAULTS:
            if name not in omitted and any(item in omitted for item in requires):
                omitted.add(name)
                changed = True
    return tuple(
        ExtensionSpec(
            name=name,
            setup=setup,
            requires=requires,
            source=SourceInfo(name=name, scope="builtin"),
        )
        for name, setup, requires in _DEFAULTS
        if name not in omitted
    )


DEFAULT_EXTENSION_NAMES = tuple(name for name, _setup, _requires in _DEFAULTS)


__all__ = ["DEFAULT_EXTENSION_NAMES", "default_extension_specs"]
