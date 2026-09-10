"""Bounded preference and fact selection from immutable published resources."""

from __future__ import annotations

from time import time

from run_agent_coding.context_window import estimate_text_tokens
from run_agent_coding.extensions import ResourceSelection, ResourceView


def select_context(view: ResourceView) -> list[ResourceSelection]:
    selected: list[ResourceSelection] = []
    used = 0
    project_heads = view.heads("project")
    for scope in ("project", "user"):
        heads = view.heads(scope)
        ordered = sorted(heads.items(), key=lambda item: (not item[0].startswith("user/"), item[0]))
        for key, version in ordered:
            if not key.startswith(("user/", "memory/", "skill/")):
                continue
            if scope == "user" and key in project_heads:
                continue
            value = view.resolve(scope, key, version)
            expires = value.metadata.get("expires_at")
            if (
                not value.metadata.get("active", False)
                or isinstance(expires, int | float)
                and expires <= time()
            ):
                continue
            title = f"Experience context: {scope}/{key}"
            if key.startswith("skill/"):
                description = str(value.metadata.get("description", ""))
                tokens = estimate_text_tokens(
                    f"Available experience Skill\n{scope}/{key} [{version}]: {description}"
                )
                if len(selected) < 64 and tokens <= 256 and used + tokens <= 1800:
                    selected.append(
                        ResourceSelection(
                            scope,
                            key,
                            version,
                            "Available experience Skill",
                            max_tokens=256,
                            presentation="index",
                        )
                    )
                    used += tokens
                continue
            tokens = estimate_text_tokens(title + "\n" + value.content)
            if tokens > 1000 or used + tokens > 1800:
                continue
            selected.append(ResourceSelection(scope, key, version, title, max_tokens=1000))
            used += tokens
    return selected
