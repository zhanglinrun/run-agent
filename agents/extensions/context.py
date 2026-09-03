"""Structured context compaction and persistent-memory extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..context.folding import (
    FOLD_SESSION_MEMORY_SYSTEM,
    build_folding_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    parse_folded_memory,
)
from ..context.memory import (
    MAX_SESSION_MEMORY_BYTES,
    VALID_TYPES,
    build_memory_prompt_section,
    format_memories_for_injection,
    save_memory,
    select_relevant_memories,
)
from ..runtime.hooks import ModelContext
from ..session import SessionReducer
from .contracts import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionEvent,
    ToolHandlerResult,
)


@dataclass
class ContextState:
    requested_reason: str | None = None


@dataclass
class MemoryState:
    surfaced: set[str] = field(default_factory=set)
    selected_queries: set[str] = field(default_factory=set)
    injected_bytes: int = 0


def setup_context(api: ExtensionAPI) -> None:
    async def session_start(_event: ExtensionEvent, context: ExtensionContext) -> None:
        context.services["context_state"] = ContextState()

    async def compact_tool(value: dict[str, Any], context: ExtensionContext) -> str:
        state = context.require("context_state")
        state.requested_reason = str(value.get("reason") or "manual context compaction")
        return f"Structured context compaction scheduled: {state.requested_reason}"

    async def compact_context(event: ExtensionEvent, context: ExtensionContext) -> ModelContext | None:
        current = event.data.get("context")
        if not isinstance(current, ModelContext):
            return None
        state = context.require("context_state")
        settings = context.task.runtime.prompt
        threshold = max(settings.keep_recent_messages + 1, settings.context_message_limit)
        if state.requested_reason is None and len(current.messages) <= threshold:
            return None
        keep_recent = max(4, settings.keep_recent_messages)
        if len(current.messages) <= keep_recent:
            state.requested_reason = None
            return None

        older = current.messages[:-keep_recent]
        recent = current.messages[-keep_recent:]
        transcript = build_openai_transcript(older)
        phase = "repair" if context.state.phase.value == "correcting" else "solve"
        if context.state.budgets.remaining_for(phase) <= 1:
            memory = fallback_folded_memory(transcript)
            fallback = True
        else:
            try:
                response = await context.side_query(
                    FOLD_SESSION_MEMORY_SYSTEM,
                    build_folding_user_prompt(transcript),
                )
                memory = parse_folded_memory(response)
                fallback = False
            except Exception:
                memory = fallback_folded_memory(transcript)
                fallback = True
        folded = format_folded_memory(memory)
        reducer = SessionReducer(context.repository)
        reducer.append_compaction(
            context.state.session_id,
            folded,
            lane_id=context.state.lane_id,
            details={
                "reason": state.requested_reason or "automatic threshold",
                "source_message_count": len(older),
                "structured": True,
                "fallback": fallback,
                "memory": memory,
            },
        )
        persisted = [{"role": "system", "content": folded}, *recent]
        for message in recent:
            reducer.append_message(
                context.state.session_id,
                message,
                lane_id=context.state.lane_id,
            )
        middleware = context.services.get("session_middleware")
        if middleware is not None and hasattr(middleware, "remember_projection"):
            middleware.remember_projection(persisted)
        state.requested_reason = None
        result = ModelContext(
            current.system,
            persisted,
            list(current.tools),
        )
        event.data["context"] = result
        return result

    async def compact_command(args: str, context: ExtensionContext) -> str:
        state = context.require("context_state")
        state.requested_reason = args.strip() or "manual command"
        messages = SessionReducer(context.repository).messages(
            context.state.session_id, lane_id=context.state.lane_id
        )
        current = ModelContext(
            context.base_prompt,
            messages,
            context.host.tool_definitions(context),
        )
        result = await compact_context(
            ExtensionEvent("context", {"context": current}), context
        )
        return (
            "Structured session compaction completed."
            if isinstance(result, ModelContext)
            else "The session is too short to compact."
        )

    api.register_tool(
        {
            "name": "compact_context",
            "description": "Compact older conversation history into episode, working, and tool memory.",
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
        compact_tool,
        prompt_snippet="`compact_context` folds older turns into structured session memory.",
    )
    api.register_command(
        "compact", compact_command, description="Compact the session into structured memory"
    )
    api.on("session_start", session_start)
    api.on("context", compact_context)


def setup_memory(api: ExtensionAPI) -> None:
    async def session_start(_event: ExtensionEvent, context: ExtensionContext) -> None:
        context.services["memory_state"] = MemoryState()

    def memory_prompt(_render: Any, _context: ExtensionContext) -> str:
        return build_memory_prompt_section()

    async def recall(event: ExtensionEvent, context: ExtensionContext) -> ModelContext | None:
        current = event.data.get("context")
        if not isinstance(current, ModelContext):
            return None
        if context.depth > 0:
            return None
        state = context.require("memory_state")
        query = next(
            (
                str(message.get("content") or "").strip()
                for message in reversed(current.messages)
                if message.get("role") == "user" and isinstance(message.get("content"), str)
            ),
            "",
        )
        query_key = query[:2000]
        if len(query.strip()) < 4 or query_key in state.selected_queries:
            return None
        phase = "repair" if context.state.phase.value == "correcting" else "solve"
        if context.state.budgets.remaining_for(phase) <= 1:
            return None
        state.selected_queries.add(query_key)
        memories = await select_relevant_memories(
            query,
            context.side_query,
            state.surfaced,
        )
        if not memories:
            return None
        remaining = max(0, MAX_SESSION_MEMORY_BYTES - state.injected_bytes)
        selected = []
        used = 0
        for item in memories:
            if item.size > remaining - used:
                continue
            selected.append(item)
            used += item.size
        if not selected:
            return None
        state.injected_bytes += used
        state.surfaced.update(item.path for item in selected)
        injected = format_memories_for_injection(selected)
        result = ModelContext(
            current.system + "\n\n" + injected,
            list(current.messages),
            list(current.tools),
        )
        event.data["context"] = result
        return result

    async def memory_save(
        value: dict[str, Any], _context: ExtensionContext
    ) -> str | ToolHandlerResult:
        memory_type = str(value.get("type") or "project")
        if memory_type not in VALID_TYPES:
            return ToolHandlerResult(
                f"Invalid memory type: {memory_type!r}",
                ok=False,
                error="invalid_memory_type",
            )
        filename = save_memory(
            str(value.get("name") or ""),
            str(value.get("description") or ""),
            memory_type,
            str(value.get("content") or ""),
        )
        return f"Saved memory: {filename}"

    api.contribute_prompt("persistent-memory", memory_prompt, priority=60)
    api.register_tool(
        {
            "name": "memory_save",
            "description": "Save durable project- or user-relevant information to project-scoped persistent memory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "description": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "enum": sorted(VALID_TYPES)},
                    "content": {"type": "string", "minLength": 1},
                },
                "required": ["name", "description", "type", "content"],
                "additionalProperties": False,
            },
        },
        memory_save,
        prompt_snippet="`memory_save` records explicit durable information outside normal workspace files.",
        prompt_guidelines=("Use `memory_save` only for durable information the user asked to retain.",),
    )
    api.on("session_start", session_start)
    api.on("context", recall)


__all__ = ["setup_context", "setup_memory"]
