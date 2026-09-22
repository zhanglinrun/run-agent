"""Verifier-gated Skill evolution extension wiring.

Memory lives in the separate ``run_agent_extensions.hermes_memory`` extension; this
package keeps only the Skill half plus the shared write guards (``threats``,
``mutation``, ``write_approval``) that both extensions import.
"""

from __future__ import annotations

from typing import Any, cast

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
)

from .candidates import ProjectProbe
from .commands_evolution import register_evolution_commands
from .config import ExperienceConfig, load_experience_config
from .evolution import EvolutionPolicy, SkillEvolution
from .stores import ExperienceStores
from .tools import register_tools

PROMPT_GUIDELINE = (
    "Published Skills are read-only to the model. Reusable procedural improvements may "
    "only be proposed as candidates with skill_manage and are not active until a host "
    "evaluation passes and the user publishes them with /evolve."
)


def setup(api: ExtensionAPI) -> None:
    holder: dict[str, Any] = {
        "stores": None,
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
            inference=context.services.inference,
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

    async def settled(event: object, context: ExtensionContext) -> None:
        del context
        if isinstance(event, AgentSettledEvent):
            holder["source_run"] = event.run_id

    def source_run() -> str:
        current = holder["source_run"]
        return current if isinstance(current, str) else ""

    api.on("session_start", cast(ExtensionHandler, start))
    api.on("agent_settled", cast(ExtensionHandler, settled))
    register_evolution_commands(api, evolution)
    register_tools(api, stores, config, evolution, source_run)
    api.add_prompt_guideline(PROMPT_GUIDELINE)
