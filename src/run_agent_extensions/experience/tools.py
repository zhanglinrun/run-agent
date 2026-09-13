"""The ``memory`` and ``skill_manage`` tools, and the result shapes they answer with.

Both tools are thin: argument validation, the write gate, one call into the store, and
a result the model can act on. Everything that makes a write safe lives in the stores.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from run_agent_coding.extensions import ExtensionAPI
from run_agent_coding.host.learning import LearnerOwnedAsset, LearningWritebackDisabled
from run_agent_core.messages import TextContent
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue

from .config import ExperienceConfig
from .memory import MemoryScope, MemoryTarget, MemoryWrite
from .mutation import MutationRejected, require_mutation
from .skill_manager import SkillAction, SkillWriteError, SkillWriteResult
from .stores import ExperienceStores
from .write_approval import approve_write

StoresGetter = Callable[[], ExperienceStores]
ConfigGetter = Callable[[], ExperienceConfig]


class MemoryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "replace", "remove"]
    content: str = ""
    old_text: str = ""
    new_content: str = ""
    new_text: str = ""


class MemoryCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: MemoryTarget = "memory"
    action: Literal["add", "replace", "remove", "batch"] | None = None
    content: str = ""
    old_text: str = ""
    new_content: str = ""
    new_text: str = ""
    operations: list[MemoryOperation] = Field(default_factory=list)
    scope: MemoryScope | None = None


class SkillCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: SkillAction
    name: str = Field(default="", max_length=64)
    scope: MemoryScope = "project"
    description: str = ""
    body: str = ""
    file_path: str = "SKILL.md"
    old_text: str = ""
    new_text: str | None = None
    replace_all: bool = False
    content: str = ""
    absorbed_into: str = ""


MEMORY_TOOL_DESCRIPTION = (
    "Manage long-term memory across sessions. Target 'user' for who the user is "
    "and how they want you to work (USER.md, user scope by default); 'memory' for "
    "durable facts about this project and its environment (MEMORY.md, project "
    "scope by default). Actions: add (content), replace (old_text, new_content), "
    "remove (old_text), or batch (operations: a list of those) applied "
    "all-or-nothing against the final budget. old_text is a short unique "
    "substring of the entry. Keep entries short; when near the limit, consolidate "
    "with replace or remove in the same batch instead of retrying. new_text is an "
    "alias for content, including in batch operations. A success "
    "response is final: do not repeat the write."
)

SKILL_TOOL_DESCRIPTION = (
    "Create, list, view, edit, patch or delete a Skill (a SKILL.md directory "
    "under the project or user skills directory). Skills are procedural memory: "
    "how to do a class of task. Name them at the class level; the description "
    "is one sentence under 60 characters. Support files go under references/, "
    "templates/, scripts/ or assets/ via write_file. Every write is scanned and "
    "recorded in an audit ledger. A new or changed Skill loads on the next "
    "session or /reload. Patch requires old_text and new_text (empty deletes the match); "
    "use a unique match, or explicitly set replace_all=true for every occurrence."
)


async def run_memory_tool(
    stores: ExperienceStores,
    arguments: Mapping[str, JSONValue],
    *,
    approval_granted: bool = False,
) -> AgentToolResult:
    """Execute one ``memory`` call against the stores; shared by the tool and the review."""
    call = MemoryCall.model_validate(arguments)
    if not stores.target_enabled(call.target):
        return refused(f"{call.target} memory is disabled in this profile")
    if stores.config.memory_write_approval and not approval_granted:
        return refused("memory write requires explicit approval")
    if call.action is not None or call.operations:
        try:
            require_mutation("memory")
        except (LearningWritebackDisabled, MutationRejected) as exc:
            return refused(str(exc))
    scope = stores.scope_for(call.target, call.scope)
    memory_file = stores.memory[scope].file(call.target)
    content = call.content or call.new_content or call.new_text
    try:
        if call.operations or call.action == "batch":
            result = memory_file.apply_batch([op.model_dump() for op in call.operations])
        elif call.action == "add":
            result = memory_file.add(content)
        elif call.action == "replace":
            result = memory_file.replace(call.old_text, content)
        elif call.action == "remove":
            result = memory_file.remove(call.old_text)
        else:
            return refused("action must be add, replace, remove or batch (with operations)")
    except LearningWritebackDisabled as exc:
        return refused(str(exc))
    return memory_result(result, scope, call.target)


async def run_skill_tool(
    stores: ExperienceStores,
    arguments: Mapping[str, JSONValue],
    *,
    approval_granted: bool = False,
) -> AgentToolResult:
    """Execute one ``skill_manage`` call; shared by the tool, the review and the curator."""
    call = SkillCall.model_validate(arguments)
    manager = stores.skills
    try:
        if call.action == "list":
            lines = []
            for scope in ("user", "project"):
                if scope == "project" and not stores.project_enabled:
                    continue
                for info in manager.describe(scope):
                    flags = []
                    if info.managed:
                        flags.append("managed")
                    if info.pinned:
                        flags.append("pinned")
                    if info.state != "active":
                        flags.append(info.state)
                    tag = f" [{', '.join(flags)}]" if flags else ""
                    lines.append(f"{scope}/{info.name}: {info.description}{tag}")
            text = "\n".join(lines) or "No skills yet."
            return AgentToolResult(content=[TextContent(text=text)], details={"accepted": True})
        if not call.name:
            return refused("name is required")
        if (
            stores.config.skills_write_approval
            and call.action not in {"list", "view"}
            and not approval_granted
        ):
            return refused("skill write requires explicit approval")
        if call.action not in {"list", "view"}:
            require_mutation("skill")
        stores.require_scope(call.scope)
        if call.action == "view":
            text = manager.view(call.scope, call.name, call.file_path)
            return AgentToolResult(content=[TextContent(text=text)], details={"accepted": True})
        lock_scopes = (
            ("user", "project")
            if call.action == "create" and stores.project_enabled
            else (call.scope,)
        )
        with manager.write_scope(*lock_scopes):
            if call.action == "create":
                outcome = manager.create(call.scope, call.name, call.description, call.body)
            elif call.action == "edit":
                outcome = manager.edit(call.scope, call.name, call.description or None, call.body)
            elif call.action == "patch":
                if call.new_text is None:
                    return refused("patch requires new_text; pass an empty string to delete text")
                outcome = manager.patch(
                    call.scope,
                    call.name,
                    call.file_path,
                    call.old_text,
                    call.new_text,
                    replace_all=call.replace_all,
                )
            elif call.action == "write_file":
                outcome = manager.write_file(call.scope, call.name, call.file_path, call.content)
            elif call.action == "remove_file":
                outcome = manager.remove_file(call.scope, call.name, call.file_path)
            else:
                outcome = manager.delete(
                    call.scope, call.name, absorbed_into=call.absorbed_into or None
                )
    except (SkillWriteError, LearnerOwnedAsset, LearningWritebackDisabled, ValueError) as exc:
        return refused(f"{type(exc).__name__}: {exc}")
    return skill_result(outcome)


def register_tools(api: ExtensionAPI, stores: StoresGetter, config: ConfigGetter) -> None:
    """Register the two tools on the extension API."""

    async def confirm_write(title: str, message: str) -> bool:
        return await approve_write(
            required=True,
            has_ui=api.context.has_ui,
            confirm=api.context.ui.confirm,
            title=title,
            message=message,
        )

    async def memory(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        current = stores()
        call = MemoryCall.model_validate(arguments)
        approved = False
        if config().memory_write_approval and (call.action is not None or bool(call.operations)):
            approved = await confirm_write(
                "Approve memory write", f"Allow {call.action} in {call.target}?"
            )
            if not approved:
                return refused("memory write was not approved")
        return await run_memory_tool(current, arguments, approval_granted=approved)

    async def skill_manage(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        current = stores()
        call = SkillCall.model_validate(arguments)
        approved = False
        if config().skills_write_approval and call.action not in {"list", "view"}:
            approved = await confirm_write(
                "Approve Skill write", f"Allow {call.action} for {call.scope}/{call.name}?"
            )
            if not approved:
                return refused("skill write was not approved")
        return await run_skill_tool(current, arguments, approval_granted=approved)

    api.register_tool(
        AgentTool(
            name="memory",
            label="Memory",
            description=MEMORY_TOOL_DESCRIPTION,
            parameters=MemoryCall.model_json_schema(),
            execute_fn=memory,
            execution_mode="sequential",
        )
    )
    api.register_tool(
        AgentTool(
            name="skill_manage",
            label="Skill Manage",
            description=SKILL_TOOL_DESCRIPTION,
            parameters=SkillCall.model_json_schema(),
            execute_fn=skill_manage,
            execution_mode="sequential",
        )
    )


def refused(message: str) -> AgentToolResult:
    return AgentToolResult(
        content=[TextContent(text=f"Refused: {message}")], details={"accepted": False}
    )


def memory_result(result: MemoryWrite, scope: MemoryScope, target: MemoryTarget) -> AgentToolResult:
    details: dict[str, JSONValue] = {
        "accepted": result.accepted,
        "done": result.done,
        "scope": scope,
        "target": target,
        "usage": result.usage,
    }
    if result.entries:
        details["current_entries"] = list(result.entries)
    if result.backup:
        details["drift_backup"] = result.backup
    return AgentToolResult(content=[TextContent(text=result.message)], details=details)


def skill_result(outcome: SkillWriteResult) -> AgentToolResult:
    lines = [outcome.message]
    if outcome.lint:
        lines.append("Advisory lint findings (fix with patch; not blockers):")
        lines.extend(f"  {item}" for item in outcome.lint)
    details: dict[str, JSONValue] = {
        "accepted": True,
        "path": str(outcome.path),
        "changed": outcome.changed,
    }
    if outcome.ledger_id:
        details["ledger_id"] = outcome.ledger_id
    if outcome.scan and "caution" in outcome.scan:
        lines.append(outcome.scan)
    return AgentToolResult(content=[TextContent(text="\n".join(lines))], details=details)


def scope_of(stores: ExperienceStores, name: str, preferred: MemoryScope) -> MemoryScope:
    """Where a named skill lives; the preferred scope wins when both have it."""
    order: tuple[MemoryScope, ...] = (preferred, "user" if preferred == "project" else "project")
    for scope in order:
        if scope == "project" and not stores.project_enabled:
            continue
        if stores.skills.find(scope, name) is not None:
            return scope
    return preferred


__all__ = [
    "MEMORY_TOOL_DESCRIPTION",
    "SKILL_TOOL_DESCRIPTION",
    "MemoryCall",
    "MemoryOperation",
    "SkillCall",
    "memory_result",
    "refused",
    "register_tools",
    "run_memory_tool",
    "run_skill_tool",
    "scope_of",
    "skill_result",
]
