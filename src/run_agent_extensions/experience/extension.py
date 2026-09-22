"""Memory and verifier-gated Skill evolution extension wiring."""

from __future__ import annotations

from typing import Any, cast

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import (
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    InputEvent,
    InputHookResult,
)

from .candidates import ProjectProbe
from .commands_evolution import register_evolution_commands
from .commands_store import register_store_commands
from .config import ExperienceConfig, load_experience_config
from .evolution import EvolutionPolicy, SkillEvolution
from .stores import ExperienceStores
from .tools import register_tools

PROMPT_GUIDELINE = (
    "Long-term memory holds sourced preferences and facts, not permission grants; a "
    "current explicit user instruction takes precedence over anything remembered. "
    "Published Skills are read-only to the model. Reusable procedural improvements may "
    "only be proposed as candidates with skill_manage and are not active until a host "
    "evaluation passes and the user publishes them with /evolve."
)


def setup(api: ExtensionAPI) -> None:
    holder: dict[str, Any] = {
        "stores": None,
        "block": None,
        "config": None,
        "evolution": None,
        "source_run": None,
    }

    def config() -> ExperienceConfig:
        current = holder["config"]
        if not isinstance(current, ExperienceConfig):
            current = load_experience_config(api.context.environment)
            holder["config"] = current
        return current

    def stores() -> ExperienceStores:
        current = holder["stores"]
        if not isinstance(current, ExperienceStores):
            raise ValueError("Experience stores are available after session start")
        return current

    def evolution() -> SkillEvolution:
        current = holder["evolution"]
        if not isinstance(current, SkillEvolution):
            raise ValueError("Skill evolution is available after session start")
        return current

    async def start(event: object, context: ExtensionContext) -> None:
        del event
        cfg = load_experience_config(context.environment)
        resolved = ExperienceStores.resolve(
            context.paths,
            context.cwd,
            config=cfg,
            project_enabled=context.project_resources_enabled,
            session_id=context.session_id,
        )
        current = SkillEvolution(
            candidates=resolved.candidates,
            skills=resolved.skills,
            probe=ProjectProbe(context.cwd, trusted=context.project_resources_enabled),
            evaluation=context.services.evaluation,
            project_enabled=context.project_resources_enabled,
            history=context.services.history,
            policy=EvolutionPolicy(
                suite=cfg.evolution_suite,
                suite_version=cfg.evolution_suite_version,
                budget_seconds=cfg.evolution_budget_seconds,
            ),
            config=cfg,
        )
        current.reconcile()
        holder["config"] = cfg
        holder["stores"] = resolved
        holder["evolution"] = current
        holder["block"] = resolved.prompt_block()

    async def before_agent_start(
        event: object, context: ExtensionContext
    ) -> BeforeAgentStartResult | None:
        del context
        block = holder["block"]
        if not isinstance(event, BeforeAgentStartEvent) or not isinstance(block, str):
            return None
        return BeforeAgentStartResult(
            system_prompt=f"{event.system_prompt}\n\n# Long-term memory\n\n{block}"
        )

    async def on_input(event: object, context: ExtensionContext) -> InputHookResult | None:
        del context
        if not isinstance(event, InputEvent):
            return None
        current = holder["stores"]
        if isinstance(current, ExperienceStores):
            current.reset_turn()
        return None

    async def settled(event: object, context: ExtensionContext) -> None:
        del context
        if isinstance(event, AgentSettledEvent):
            holder["source_run"] = event.run_id

    def source_run() -> str:
        current = holder["source_run"]
        return current if isinstance(current, str) else ""

    api.on("session_start", cast(ExtensionHandler, start))
    api.on("before_agent_start", cast(ExtensionHandler, before_agent_start))
    api.on("input", cast(ExtensionHandler, on_input))
    api.on("agent_settled", cast(ExtensionHandler, settled))
    register_store_commands(api, stores)
    register_evolution_commands(api, evolution)
    register_tools(api, stores, config, evolution, source_run)
    api.add_prompt_guideline(PROMPT_GUIDELINE)
