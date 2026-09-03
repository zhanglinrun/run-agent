"""Bounded sub-agent extension with inherited permission ceilings."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any, Iterable

from ..collaboration import get_role_spec
from ..runtime import AgentCore, CompositeAgentHooks
from ..harness.middleware import BudgetMiddleware, SessionTaskMiddleware
from .contracts import ExtensionAPI, ExtensionContext
from .host import ExtensionToolExecutor
from .policy import PermissionState


class SubagentService:
    def __init__(self, context: ExtensionContext) -> None:
        self.context = context

    async def run(
        self,
        *,
        role_name: str,
        prompt: str,
        description: str,
        allowed_tools: Iterable[str] | None = None,
    ) -> str:
        parent_context = self.context.host.current_context
        if parent_context.depth >= 1:
            raise RuntimeError("sub-agents cannot create another sub-agent")
        try:
            role = get_role_spec(role_name)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

        remaining = (
            parent_context.state.budgets.solve_remaining
            if parent_context.state.phase.value == "solving"
            else parent_context.state.budgets.repair_remaining
        )
        child_turns = min(4, max(0, remaining - 1))
        if child_turns <= 0:
            raise RuntimeError("shared task turn budget is exhausted")

        parent = parent_context.repository.latest_entry(
            parent_context.state.session_id,
            lane_id=parent_context.state.lane_id,
        )
        lane_id = parent_context.repository.create_lane(
            parent_context.state.session_id,
            name=f"{role.name}:{description or 'task'}",
            parent_lane_id=parent_context.state.lane_id,
            parent_entry_id=parent.id if parent else None,
        )
        child_state = replace(parent_context.state, lane_id=lane_id)
        parent_names = set(parent_context.host._tool_names_for(parent_context))
        child_names = parent_names & set(role.allowed_tools)
        if allowed_tools is not None:
            child_names &= {str(name) for name in allowed_tools}
        child_names.discard("agent")
        if role.name != "coder":
            child_names.discard("run_shell")

        parent_permission = parent_context.require("permission_state")
        child_permission = PermissionState(
            mode=parent_permission.mode,
            plan_file=parent_permission.plan_file,
            mode_before_plan=parent_permission.mode_before_plan,
            read_only=parent_permission.read_only or role.name != "coder",
        )
        child_services = dict(parent_context.services)
        child_services["permission_state"] = child_permission
        child_context = ExtensionContext(
            task=parent_context.task,
            state=child_state,
            repository=parent_context.repository,
            journal=parent_context.journal,
            provider=parent_context.provider,
            execution=parent_context.execution,
            artifact_root=parent_context.artifact_root,
            trace=parent_context.trace,
            base_prompt=(
                role.system_prompt
                + "\n\n# Parent task contract\n"
                + parent_context.base_prompt
            ),
            services=child_services,
            active_tool_names=frozenset(child_names),
            tool_ceiling_names=frozenset(child_names),
            depth=parent_context.depth + 1,
            host=parent_context.host,
        )
        child_middleware = SessionTaskMiddleware(
            child_state,
            child_context.repository,
            child_context.journal,
            trace=child_context.trace,
        )
        child_context.services["session_middleware"] = child_middleware
        child = AgentCore(
            provider=child_context.provider,
            tool_executor=ExtensionToolExecutor(
                child_context.host, allowed_names=child_names
            ),
            system_prompt=child_context.base_prompt,
            tools=child_context.host.tool_definitions(child_context),
            hooks=CompositeAgentHooks(
                [child_middleware, child_context.host, BudgetMiddleware(parent_context.state)]
            ),
            max_turns=child_turns,
        )
        with child_context.host.use_context(child_context):
            result = await child.run(prompt, max_turns=child_turns)
        child_middleware.flush_context(child.context)
        parent_context.journal.observe()
        return json.dumps(
            {
                "role": role.name,
                "lane_id": lane_id,
                "answer": result.text,
                "turns": result.turns,
                "allowed_tools": sorted(child_names),
                "permission_mode": child_permission.mode,
            },
            ensure_ascii=False,
        )


def setup_subagents(api: ExtensionAPI) -> None:
    async def session_start(_event: Any, context: ExtensionContext) -> None:
        context.services["subagents"] = SubagentService(context)

    async def run_agent(value: dict[str, Any], context: ExtensionContext) -> str:
        service = context.require("subagents")
        return await service.run(
            role_name=str(value.get("type") or "coder"),
            description=str(value.get("description") or "task"),
            prompt=str(value.get("prompt") or ""),
        )

    def prompt(_render: Any, _context: ExtensionContext) -> str:
        from ..collaboration.roles import build_agent_descriptions

        return build_agent_descriptions()

    api.register_tool(
        {
            "name": "agent",
            "description": "Launch one bounded Coder, Reviewer, or Verifier child lane.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "prompt": {"type": "string", "minLength": 1},
                    "type": {
                        "type": "string",
                        "enum": ["coder", "reviewer", "verifier"],
                    },
                },
                "required": ["description", "prompt"],
                "additionalProperties": False,
            },
        },
        run_agent,
        prompt_snippet="`agent` delegates one bounded task to a non-recursive collaboration role.",
        prompt_guidelines=(
            "Use `agent` only when delegation narrows or parallelizes a concrete subproblem.",
        ),
    )
    api.contribute_prompt("collaboration-roles", prompt, priority=70)
    api.on("session_start", session_start)


__all__ = ["SubagentService", "setup_subagents"]
