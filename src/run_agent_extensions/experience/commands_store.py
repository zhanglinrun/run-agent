"""The ``/memory`` and ``/skillset`` commands: hand-driven memory edits and Skill upkeep."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import cast

from run_agent_coding.extensions import ExtensionAPI, ExtensionCommandContext
from run_agent_coding.host.learning import LearningWritebackDisabled

from .curator import Curator
from .memory import MemoryScope, MemoryTarget
from .mutation import MutationRejected, require_mutation
from .skill_manager import SkillWriteError
from .stores import ExperienceStores
from .tools import scope_of
from .write_approval import approve_write

MEMORY_USAGE = (
    "/memory show; /memory add <user|memory> <content>; "
    "/memory replace <user|memory> <old_text> <new_content>; "
    "/memory remove <user|memory> <old_text> [--scope project|user]"
)
SKILL_USAGE = (
    "/skillset list; /skillset view <name> [file]; "
    "/skillset pin|unpin|adopt|restore <name>; /skillset ledger [name]; "
    "/skillset rollback <entry-id>; /skillset archived [--scope project|user]"
)


def register_store_commands(
    api: ExtensionAPI,
    stores: Callable[[], ExperienceStores],
    curator: Callable[[], Curator],
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

    async def skill_command(args: str, context: ExtensionCommandContext) -> str:
        words = shlex.split(args)
        if not words:
            return SKILL_USAGE
        scope: MemoryScope = "project"
        if "--scope" in words:
            index = words.index("--scope")
            if index + 1 >= len(words) or words[index + 1] not in {"project", "user"}:
                raise ValueError("--scope needs project or user")
            scope = cast(MemoryScope, words[index + 1])
            del words[index : index + 2]
        action, *parts = words
        current = stores()
        manager = current.skills
        if action == "list":
            lines = []
            for memory_scope in ("user", "project"):
                if memory_scope == "project" and not current.project_enabled:
                    continue
                for info in manager.describe(memory_scope):
                    marks = "".join(m for m, on in (("M", info.managed), ("P", info.pinned)) if on)
                    lines.append(
                        f"{memory_scope}/{info.name} [{info.state}{' ' + marks if marks else ''}; "
                        f"uses={info.use_count}]: {info.description}"
                    )
            return "\n".join(lines) or "No skills yet."
        if action == "archived":
            names = manager.usage[scope].archived_names()
            return "\n".join(names) or f"No archived skills in the {scope} scope."
        if action == "ledger":
            entries = manager.ledger[scope].entries(skill=parts[0] if parts else None, limit=20)
            return (
                "\n".join(
                    f"{e.id}  {e.timestamp[:19]}  {e.actor:<8} {e.action:<12} {e.skill}"
                    for e in entries
                )
                or "The ledger is empty."
            )
        if not parts:
            return SKILL_USAGE
        name = parts[0]
        scope = scope_of(current, name, scope)
        if action == "view":
            try:
                return manager.view(scope, name, parts[1] if len(parts) > 1 else "SKILL.md")
            except SkillWriteError as exc:
                return f"Refused: {exc}"
        if action in {"pin", "unpin", "adopt", "restore", "rollback"}:
            if current.config.skills_write_approval:
                approved = await approve_write(
                    required=True,
                    has_ui=context.api.context.has_ui,
                    confirm=context.api.context.ui.confirm,
                    title="Approve Skill write",
                    message=f"Allow {action} for {scope}/{name}?",
                )
                if not approved:
                    return "Refused: Skill write was not approved"
            try:
                require_mutation("skill")
            except (LearningWritebackDisabled, MutationRejected) as exc:
                return f"Refused: {exc}"
        if action in {"pin", "unpin"}:
            return curator().pin(scope, name, action == "pin")
        if action == "adopt":
            return curator().adopt(scope, name)
        if action == "restore":
            return curator().restore(scope, name)
        if action == "rollback":
            with manager.write_scope(scope):
                ok, message = manager.ledger[scope].rollback(name)
            return message if ok else f"Refused: {message}"
        return SKILL_USAGE

    api.register_command(
        "memory", memory_command, description="Show or edit MEMORY.md and USER.md."
    )
    api.register_command(
        "skillset",
        skill_command,
        description="Manage the Skill library: pin, adopt, restore, ledger, rollback.",
    )


__all__ = ["MEMORY_USAGE", "SKILL_USAGE", "register_store_commands"]
