"""Foreground memory updates and read/propose-only Skill management."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from run_agent_coding.extensions import ExtensionAPI
from run_agent_coding.host.learning import LearningWritebackDisabled
from run_agent_core.messages import TextContent
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue

from .candidates import CandidateError, CandidateOperation
from .config import ExperienceConfig
from .evolution import SkillEvolution
from .memory import MemoryScope, MemoryTarget, MemoryWrite
from .mutation import MutationRejected, require_mutation
from .skill_manager import SkillAction, SkillWriteError
from .stores import ExperienceStores
from .write_approval import approve_write

StoresGetter = Callable[[], ExperienceStores]
ConfigGetter = Callable[[], ExperienceConfig]
EvolutionGetter = Callable[[], SkillEvolution]
SourceRunGetter = Callable[[], str]


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


class SkillOperationCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "delete", "replace"]
    old_text: str = ""
    new_text: str = ""


class SkillClaimCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    probe_paths: list[str] = Field(min_length=1)


class SkillCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: SkillAction
    name: str = Field(default="", max_length=64)
    scope: MemoryScope = "project"
    file_path: str = "SKILL.md"
    operations: list[SkillOperationCall] = Field(default_factory=list, max_length=8)
    claims: list[SkillClaimCall] = Field(default_factory=list)
    candidate_content: str | None = None


MEMORY_TOOL_DESCRIPTION = (
    "Manage long-term memory across sessions. Target 'user' for who the user is "
    "and how they want you to work (USER.md, user scope by default); 'memory' for "
    "durable facts about this project and its environment (MEMORY.md, project scope "
    "by default). Actions: add, replace, remove, or an all-or-nothing batch."
)

SKILL_TOOL_DESCRIPTION = (
    "Inspect published Skills or propose one immutable candidate. Actions: list; view "
    "with name/scope/file_path; propose with name, scope, at most 8 ordered add/delete/replace "
    "operations, optional candidate_content, and optional project "
    "claims backed by relative probe_paths. Total changed text is limited to 2000 "
    "characters. Propose never edits a published Skill. Existing Skills must already be "
    "evolution-owned and unpinned; otherwise the user must run /evolve adopt. A candidate "
    "stays cold when no host EvaluationService is available."
)


async def run_memory_tool(
    stores: ExperienceStores,
    arguments: Mapping[str, JSONValue],
    *,
    approval_granted: bool = False,
) -> AgentToolResult:
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
            result = memory_file.apply_batch(
                [operation.model_dump() for operation in call.operations]
            )
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
    evolution: SkillEvolution | None = None,
    source_session: str = "",
    source_run: str = "",
    approval_granted: bool = False,
) -> AgentToolResult:
    call = SkillCall.model_validate(arguments)
    manager = stores.skills
    try:
        if call.action == "list":
            lines: list[str] = []
            for scope in ("user", "project"):
                if scope == "project" and not stores.project_enabled:
                    continue
                for info in manager.describe(scope):
                    flags: list[str] = []
                    if info.managed:
                        flags.append("evolution-owned")
                    if info.pinned:
                        flags.append("pinned")
                    tag = f" [{', '.join(flags)}]" if flags else ""
                    lines.append(f"{scope}/{info.name}: {info.description}{tag}")
            text = "\n".join(lines) or "No skills yet."
            return AgentToolResult(content=[TextContent(text=text)], details={"accepted": True})
        if not call.name:
            return refused("name is required")
        stores.require_scope(call.scope)
        if call.action == "view":
            text = manager.view(call.scope, call.name, call.file_path)
            return AgentToolResult(content=[TextContent(text=text)], details={"accepted": True})
        if evolution is None:
            return refused("Skill evolution is not available")
        if stores.config.skills_write_approval and not approval_granted:
            return refused("candidate proposal requires explicit approval")
        require_mutation("candidate")
        if not source_session:
            return refused("candidate source session is unavailable")
        if not source_run:
            return refused("propose requires a previously committed source run")
        candidate = await evolution.propose(
            scope=call.scope,
            name=call.name,
            source_session=source_session,
            source_run=source_run,
            operations=tuple(
                CandidateOperation(operation.action, operation.old_text, operation.new_text)
                for operation in call.operations
            ),
            claims=tuple((claim.text, tuple(claim.probe_paths)) for claim in call.claims),
            candidate_content=call.candidate_content,
        )
    except (
        CandidateError,
        SkillWriteError,
        LearningWritebackDisabled,
        MutationRejected,
        ValueError,
    ) as exc:
        return refused(f"{type(exc).__name__}: {exc}")
    report = f"; report={candidate.report_id}" if candidate.report_id else ""
    return AgentToolResult(
        content=[
            TextContent(
                text=(
                    f"Proposed candidate {candidate.candidate_id} for "
                    f"{candidate.scope}/{candidate.name}; status={candidate.status}{report}."
                )
            )
        ],
        details={
            "accepted": True,
            "candidate_id": candidate.candidate_id,
            "status": candidate.status,
            "candidate_digest": candidate.candidate_digest,
            "report_id": candidate.report_id,
        },
    )


def register_tools(
    api: ExtensionAPI,
    stores: StoresGetter,
    config: ConfigGetter,
    evolution: EvolutionGetter,
    source_run: SourceRunGetter,
) -> None:
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
        del tool_call_id, signal, on_update
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
        del tool_call_id, signal, on_update
        current = stores()
        call = SkillCall.model_validate(arguments)
        approved = False
        if config().skills_write_approval and call.action == "propose":
            approved = await confirm_write(
                "Approve Skill candidate", f"Propose a candidate for {call.scope}/{call.name}?"
            )
            if not approved:
                return refused("candidate proposal was not approved")
        return await run_skill_tool(
            current,
            arguments,
            evolution=evolution(),
            source_session=api.context.session_id or "",
            source_run=source_run(),
            approval_granted=approved,
        )

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


def scope_of(stores: ExperienceStores, name: str, preferred: MemoryScope) -> MemoryScope:
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
    "SkillClaimCall",
    "SkillOperationCall",
    "memory_result",
    "refused",
    "register_tools",
    "run_memory_tool",
    "run_skill_tool",
    "scope_of",
]
