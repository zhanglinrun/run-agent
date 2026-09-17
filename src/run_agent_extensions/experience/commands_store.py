"""The ``/memory`` command: hand-driven memory edits."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import cast

from run_agent_coding.extensions import ExtensionAPI, ExtensionCommandContext
from run_agent_coding.host.learning import LearningWritebackDisabled

from .memory import MemoryScope, MemoryTarget
from .mutation import MutationRejected, require_mutation
from .stores import ExperienceStores
from .write_approval import approve_write

MEMORY_USAGE = (
    "/memory show; /memory add <user|memory> <content>; "
    "/memory replace <user|memory> <old_text> <new_content>; "
    "/memory remove <user|memory> <old_text> [--scope project|user]"
)


def register_store_commands(
    api: ExtensionAPI,
    stores: Callable[[], ExperienceStores],
) -> None:
    async def memory_command(args: str, context: ExtensionCommandContext) -> str:
        words = shlex.split(args)
        if not words:
            return MEMORY_USAGE
        scope: MemoryScope | None = None
        if "--scope" in words:
            index = words.index("--scope")
            if index + 1 >= len(words) or words[index + 1] not in {"project", "user"}:
                raise ValueError("--scope needs project or user")
            scope = cast(MemoryScope, words[index + 1])
            del words[index : index + 2]
        action, *parts = words
        current = stores()
        if action == "show":
            lines: list[str] = []
            for memory_scope in ("user", "project"):
                if memory_scope == "project" and not current.project_enabled:
                    continue
                store = current.memory[memory_scope]
                for target in ("user", "memory"):
                    memory_file = store.file(target)
                    lines.append(f"[{memory_scope}] {memory_file.path} ({memory_file.usage})")
                    lines.extend(f"  - {entry}" for entry in memory_file.entries)
            return "\n".join(lines)
        if action not in {"add", "replace", "remove"} or len(parts) < 2:
            return MEMORY_USAGE
        if parts[0] not in {"user", "memory"}:
            raise ValueError("Memory target must be user or memory")
        target = cast(MemoryTarget, parts[0])
        if current.config.memory_write_approval:
            approved = await approve_write(
                required=True,
                has_ui=context.api.context.has_ui,
                confirm=context.api.context.ui.confirm,
                title="Approve memory write",
                message=f"Allow {action} in {target}?",
            )
            if not approved:
                return "Refused: memory write was not approved"
        try:
            require_mutation("memory")
        except (LearningWritebackDisabled, MutationRejected) as exc:
            return f"Refused: {exc}"
        memory_file = current.memory[current.scope_for(target, scope)].file(target)
        try:
            if action == "add":
                result = memory_file.add(" ".join(parts[1:]))
            elif action == "replace":
                if len(parts) < 3:
                    return MEMORY_USAGE
                result = memory_file.replace(parts[1], " ".join(parts[2:]))
            else:
                result = memory_file.remove(" ".join(parts[1:]))
        except LearningWritebackDisabled as exc:
            return f"Refused: {exc}"
        return result.message

    api.register_command(
        "memory", memory_command, description="Show or edit MEMORY.md and USER.md."
    )


__all__ = ["MEMORY_USAGE", "register_store_commands"]
