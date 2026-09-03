"""Bounded Coder / Reviewer / Verifier roles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str
    allowed_tools: frozenset[str]
    system_prompt: str


_READ = frozenset({"read_file", "list_files", "grep_search", "compact_context"})

ROLE_SPECS: dict[str, RoleSpec] = {
    "coder": RoleSpec(
        "coder",
        "Implement a scoped patch and return the changed files and rationale.",
        _READ | frozenset({"write_file", "edit_file", "run_shell"}),
        "You are the Coder. Implement only the assigned change. Use typed file tools for edits, run focused checks, and report changed files. Do not approve your own completion gate.",
    ),
    "reviewer": RoleSpec(
        "reviewer",
        "Review the plan and diff independently without modifying files.",
        _READ,
        "You are the Reviewer. Stay read-only. Inspect the proposed plan, changed files, edge cases, and policy violations. Return concrete findings; do not modify files or execute project code.",
    ),
    "verifier": RoleSpec(
        "verifier",
        "Inspect deterministic verification evidence without modifying files.",
        _READ,
        "You are the Verifier. Stay read-only. Inspect the smallest relevant syntax, test, lint, typecheck, and build evidence already present in the session or workspace. Report concrete gaps and failures; do not execute project code.",
    ),
}

def get_role_spec(name: str) -> RoleSpec:
    if name not in ROLE_SPECS:
        raise KeyError(f"unknown collaboration role: {name}")
    return ROLE_SPECS[name]


def get_available_agent_types() -> list[dict[str, str]]:
    """Return the built-in collaboration roles for prompt/UI descriptions."""
    return [
        {"name": role.name, "description": role.description}
        for role in ROLE_SPECS.values()
    ]


def build_agent_descriptions() -> str:
    lines = ["\n# Collaboration Roles", ""]
    for role in get_available_agent_types():
        lines.append(f"- **{role['name']}**: {role['description']}")
    return "\n".join(lines)
