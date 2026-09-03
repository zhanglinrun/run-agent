"""Skills discovery, constrained execution, and online evolution extensions."""

from __future__ import annotations

import json
from typing import Any

from ..evolution.lifecycle import online_ingest, record_skill_usage_judgments
from ..evolution.evaluator import format_online_skill_eval_async
from ..evolution.skills import (
    build_skill_descriptions,
    create_skill,
    evolve_skill,
    execute_skill,
    format_retrieved_skill_context,
)
from .contracts import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionEvent,
    PromptRenderContext,
    ToolHandlerResult,
)


def _definition(name: str) -> dict[str, Any]:
    from ..tools.registry import tool_definitions

    for item in tool_definitions:
        if item.get("name") == name:
            return {key: value for key, value in item.items() if key != "deferred"}
    raise KeyError(name)


def setup_skills(api: ExtensionAPI) -> None:
    def prompt(render: PromptRenderContext, context: ExtensionContext) -> str:
        catalog = build_skill_descriptions()
        retrieved, reference = format_retrieved_skill_context(
            render.latest_user_message, limit=3
        )
        context.services["retrieved_skill_reference"] = reference
        context.services["retrieved_skill_hits"] = (
            list(reference.get("all_hits") or []) if reference else []
        )
        return "\n\n".join(part for part in (catalog, retrieved) if part)

    async def skill(
        value: dict[str, Any], context: ExtensionContext
    ) -> str | ToolHandlerResult:
        result = execute_skill(
            str(value.get("skill_name") or ""), value.get("args", "")
        )
        if result is None:
            return ToolHandlerResult(
                f"Unknown skill: {value.get('skill_name', '')}",
                ok=False,
                error="unknown_skill",
            )
        used = context.services.setdefault("used_skill_names", set())
        if isinstance(used, set):
            used.add(str(value.get("skill_name") or ""))
        skill_prompt = str(result.get("prompt") or "")
        allowed = result.get("allowed_tools")
        if result.get("context") == "fork":
            service = context.require("subagents")
            return await service.run(
                role_name="coder",
                description=f"skill:{value.get('skill_name', '')}",
                prompt=skill_prompt,
                allowed_tools=allowed,
            )
        if allowed is not None:
            ceiling = set(context.host._tool_names_for(context)) & {
                str(name) for name in allowed
            }
            ceiling = frozenset(ceiling)
            context.active_tool_names = ceiling
            context.tool_ceiling_names = ceiling
        return f"[Skill activated]\n\n{skill_prompt}"

    async def skill_evolve(
        value: dict[str, Any], _context: ExtensionContext
    ) -> ToolHandlerResult:
        result = evolve_skill(
            skill_name=str(value.get("skill_name") or ""),
            lesson=str(value.get("lesson") or ""),
            rationale=str(value.get("rationale") or ""),
            target=str(value.get("target") or "active"),
        )
        return ToolHandlerResult(
            json.dumps(result, ensure_ascii=False, indent=2),
            ok=bool(result.get("ok")),
            error=None
            if result.get("ok")
            else str(result.get("error") or "skill_evolve_failed"),
        )

    async def skill_create(
        value: dict[str, Any], _context: ExtensionContext
    ) -> ToolHandlerResult:
        result = create_skill(
            name=str(value.get("name") or ""),
            description=str(value.get("description") or ""),
            instructions=str(value.get("instructions") or ""),
            when_to_use=str(value.get("when_to_use") or ""),
            target=str(value.get("target") or "project"),
            context=str(value.get("context") or "inline"),
            user_invocable=bool(value.get("user_invocable", False)),
            allowed_tools=value.get("allowed_tools"),
            evidence=str(value.get("evidence") or ""),
        )
        return ToolHandlerResult(
            json.dumps(result, ensure_ascii=False, indent=2),
            ok=bool(result.get("ok")),
            error=None
            if result.get("ok")
            else str(result.get("error") or "skill_create_failed"),
        )

    async def after_run(_event: ExtensionEvent, context: ExtensionContext) -> None:
        hits = context.services.pop("retrieved_skill_hits", [])
        used = context.services.pop("used_skill_names", set())
        if not isinstance(hits, list) or not hits:
            return
        used_names = used if isinstance(used, set) else set()
        record_skill_usage_judgments(
            [
                {
                    "name": str(hit.get("name") or ""),
                    "source": str(hit.get("source") or ""),
                    "skill_dir": str(hit.get("skill_dir") or ""),
                    "score": float(hit.get("score", 0.0) or 0.0),
                    "relevant": True,
                    "used": str(hit.get("name") or "") in used_names,
                    "reason": "retrieved for the current user request",
                }
                for hit in hits
                if isinstance(hit, dict) and str(hit.get("name") or "")
            ]
        )

    api.contribute_prompt("skills", prompt, priority=80)
    api.on("after_run", after_run)
    api.register_tool(
        _definition("skill"),
        skill,
        prompt_snippet="`skill` activates a matching SKILL.md workflow and enforces its tool ceiling.",
    )
    api.register_tool(
        _definition("skill_evolve"),
        skill_evolve,
        prompt_snippet="`skill_evolve` records an explicitly requested durable lesson.",
        deferred=True,
    )
    api.register_tool(
        _definition("skill_create"),
        skill_create,
        prompt_snippet="`skill_create` creates an explicitly requested reusable workflow.",
        deferred=True,
    )


def setup_skill_evolution(api: ExtensionAPI) -> None:
    async def evaluate_command(_args: str, context: ExtensionContext) -> str:
        return await format_online_skill_eval_async(side_query=context.side_query)

    async def before_run(_event: ExtensionEvent, context: ExtensionContext) -> None:
        pending = context.latest_state("skill-pending")
        permission = context.require("permission_state")
        if (
            not isinstance(pending, dict)
            or pending.get("consumed")
            or permission.mode == "plan"
            or context.state.budgets.solve_remaining <= 1
        ):
            return

        async def confirm_write(summary: str) -> bool:
            if permission.mode == "bypassPermissions":
                return True
            callback = context.task.runtime.permissions.confirm
            if callback is None:
                return False
            value = callback(summary)
            if hasattr(value, "__await__"):
                value = await value
            return bool(value)

        messages = list(pending.get("messages") or [])
        messages.append({"role": "user", "content": context.task.prompt})
        try:
            result = await online_ingest(
                messages=messages,
                side_query=context.side_query,
                retrieved_reference=pending.get("retrieved_reference"),
                confirm_write=confirm_write,
                target="project",
            )
            context.append_state(
                "skill-pending",
                {"consumed": True, "result": result},
            )
        except Exception as exc:
            context.append_state(
                "skill-pending",
                {"consumed": True, "error": f"{type(exc).__name__}: {exc}"},
            )

    async def after_run(_event: ExtensionEvent, context: ExtensionContext) -> None:
        permission = context.require("permission_state")
        if permission.mode == "plan":
            return
        if not context.task.prompt.strip() or not context.outcome.final_text.strip():
            return
        context.append_state(
            "skill-pending",
            {
                "consumed": False,
                "messages": [
                    {"role": "user", "content": context.task.prompt},
                    {"role": "assistant", "content": context.outcome.final_text},
                ],
                "retrieved_reference": context.services.get(
                    "retrieved_skill_reference"
                ),
            },
        )

    api.register_command(
        "skill-eval",
        evaluate_command,
        description="Evaluate pending and active Skill candidates.",
    )
    api.on("before_run", before_run)
    api.on("after_run", after_run)


__all__ = ["setup_skill_evolution", "setup_skills"]
