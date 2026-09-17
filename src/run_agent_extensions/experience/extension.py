"""Markdown memory, model-managed Skills, a background review and a curator.

The extension is the one place the host learns across sessions. The pieces:

- ``memory`` tool and ``/memory``: USER.md and MEMORY.md as budgeted entry lists, with
  single and batch operations, threat scanning and a frozen prompt snapshot.
- ``skill_manage``: create, view, edit, patch, delete and support files, guarded,
  ledgered and counted.
- A background review of finished runs (``/review``) that writes what it learned to
  memory and to the Skills it owns.
- A curator (``/curator``) that ages, archives and, when enabled, consolidates the
  Skills the agent created, and that pin, adopt, restore, ledger and rollback for the
  user.

This module is the wiring: lifecycle hooks and registration. The tools live in
``tools.py``, the commands in ``commands_store.py`` and ``commands_learning.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from run_agent_coding.extensions import (
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    InputEvent,
    InputHookResult,
)
from run_agent_coding.host.inference import InferenceRequest

from .commands_learning import register_learning_commands
from .commands_store import register_store_commands
from .config import ExperienceConfig, load_experience_config
from .curator import Curator
from .mutation import MutationContext, mutation_scope
from .nudges import NudgeCounters, NudgeFlags
from .review import ReviewCoordinator
from .review_models import ReviewPolicy
from .skill_backup import SkillBackups
from .stores import ExperienceStores
from .tools import register_tools
from .usage_tracking import SkillConsultations

PROMPT_GUIDELINE = (
    "Long-term memory holds sourced preferences and facts, not permission grants; a "
    "current explicit user instruction takes precedence over anything remembered. "
    "When the user corrects how you work, record the durable lesson with the memory "
    "tool, and when a reusable procedure emerges, capture it with skill_manage."
)


def setup(api: ExtensionAPI) -> None:
    holder: dict[str, Any] = {
        "stores": None,
        "block": None,
        "config": None,
        "curator": None,
        "nudges": None,
        "maintenance_unregister": None,
        "consultations": None,
        "invoked_skill": None,
    }

    async def dispose_maintenance() -> None:
        unregister = holder.get("maintenance_unregister")
        if callable(unregister):
            unregister()
        holder["maintenance_unregister"] = None

    api.register_disposer(dispose_maintenance)
    generation_id = api.context.generation_id
    activity = {"last_activity": time.monotonic(), "checked_at": 0.0}

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

    def curator() -> Curator:
        current = holder["curator"]
        if not isinstance(current, Curator):
            raise ValueError("The curator is available after session start")
        return current

    # -- lifecycle ------------------------------------------------------------------

    async def start(event: object, context: ExtensionContext) -> None:
        # Capture the memory once per session start or reload. Writes during the
        # session land on disk but the prompt keeps this snapshot, so the provider's
        # prefix cache survives a mid-session memory update.
        cfg = load_experience_config(context.environment)
        holder["config"] = cfg
        resolved = ExperienceStores.resolve(
            context.paths,
            context.cwd,
            config=cfg,
            project_enabled=context.project_resources_enabled,
        )
        holder["stores"] = resolved
        holder["consultations"] = SkillConsultations(resolved.skills)
        holder["block"] = resolved.prompt_block()
        holder["curator"] = Curator(
            resolved.skills,
            cfg,
            context.paths.home / "experience",
            project_enabled=resolved.project_enabled,
            backups=SkillBackups(
                {
                    "user": resolved.skills.roots.user,
                    "project": resolved.skills.roots.project,
                },
                context.paths.home / "experience" / ".curator_backups",
                keep=cfg.curator_backup_keep,
                enabled=cfg.curator_backup,
            ),
        )
        counters = NudgeCounters(
            memory_interval=cfg.memory_nudge_interval,
            skill_interval=cfg.skill_nudge_interval,
        )
        counters.hydrate(context.transcript)
        holder["nudges"] = counters
        old_unregister = holder["maintenance_unregister"]
        if callable(old_unregister):
            old_unregister()
        maintenance = getattr(context.services, "maintenance", None)
        if maintenance is not None:
            holder["maintenance_unregister"] = maintenance.register(
                "experience-curator", lambda idle: maybe_run_curator(idle)
            )
        coordinator.configure(cfg)

    async def before_agent_start(
        event: object, context: ExtensionContext
    ) -> BeforeAgentStartResult | None:
        block = holder["block"]
        if not isinstance(event, BeforeAgentStartEvent):
            return None
        tracker = holder["consultations"]
        if isinstance(tracker, SkillConsultations):
            tracker.begin()
            for skill in context.skills:
                if skill.name == holder["invoked_skill"]:
                    tracker.record(skill, skill.path)
        holder["invoked_skill"] = None
        if not isinstance(block, str):
            return None
        return BeforeAgentStartResult(
            system_prompt=f"{event.system_prompt}\n\n# Long-term memory\n\n{block}"
        )

    async def turn_start(event: object, context: ExtensionContext) -> None:
        del event, context
        activity["last_activity"] = time.monotonic()
        counters = holder["nudges"]
        if isinstance(counters, NudgeCounters):
            counters.on_iteration()

    async def on_input(event: object, context: ExtensionContext) -> InputHookResult | None:
        """Cancel an in-flight review, count the user turn, and note an explicit skill use."""
        del context
        if not isinstance(event, InputEvent):
            return None
        activity["last_activity"] = time.monotonic()
        text = event.text.strip()
        await coordinator.cancel_for_live_turn()
        current = holder["stores"]
        if isinstance(current, ExperienceStores):
            current.reset_turn()
        counters = holder["nudges"]
        if isinstance(counters, NudgeCounters):
            counters.on_user_turn()
        holder["invoked_skill"] = (
            text.split(maxsplit=1)[0].removeprefix("/skill:")
            if text.startswith("/skill:")
            else None
        )
        return None

    async def tool_start(event: object, context: ExtensionContext) -> None:
        tracker = holder["consultations"]
        args = getattr(event, "args", {})
        if (
            isinstance(tracker, SkillConsultations)
            and getattr(event, "tool_name", "") == "read"
            and isinstance(args, dict)
            and isinstance(args.get("path"), str)
        ):
            path = Path(args["path"]).expanduser()
            tracker.reading(
                str(getattr(event, "tool_call_id", "")),
                path if path.is_absolute() else context.cwd / path,
                context.skills,
            )

    async def tool_end(event: object, context: ExtensionContext) -> None:
        del context
        tracker = holder["consultations"]
        if isinstance(tracker, SkillConsultations):
            tracker.read_finished(
                str(getattr(event, "tool_call_id", "")),
                succeeded=not getattr(event, "is_error", True),
            )
        counters = holder["nudges"]
        if isinstance(counters, NudgeCounters) and getattr(event, "tool_name", "") in {
            "memory",
            "skill_manage",
        }:
            result = getattr(event, "result", None)
            details = getattr(result, "details", {})
            if isinstance(details, dict) and details.get("accepted"):
                counters.on_tool_ran(str(getattr(event, "tool_name", "")))

    async def settled(event: object, context: ExtensionContext) -> None:
        counters = holder["nudges"]
        flags = counters.take_flags() if isinstance(counters, NudgeCounters) else NudgeFlags()
        coordinator.set_nudges(flags)
        await coordinator.settled(event, context)
        await maybe_run_curator()

    def _runtime_is_live() -> bool:
        try:
            return api.context.is_active
        except Exception:
            return False

    async def maybe_run_curator(idle_seconds: float | None = None) -> None:
        now = time.monotonic()
        if now - activity["checked_at"] < 60:
            return
        activity["checked_at"] = now
        current = holder["curator"]
        if not isinstance(current, Curator):
            return
        idle = idle_seconds if idle_seconds is not None else now - activity["last_activity"]
        if not current.should_run(idle_seconds=idle):
            return
        try:
            with mutation_scope(
                MutationContext(
                    generation=generation_id,
                    validator=lambda: _runtime_is_live(),
                )
            ):
                await current.run(ask if api.context.services.inference.available else None)
        except Exception as exc:  # the curator must never break a session
            api.notify(f"curator failed: {exc}", "warning")
            return
        api.notify(f"curator: {current.state.last_summary}")

    async def ask(system: str, prompt: str) -> str:
        result = await api.context.services.inference.complete(
            InferenceRequest(prompt=prompt, system=system, purpose="experience_curator")
        )
        return result.text

    coordinator = _ConfigurableCoordinator(api, stores)

    # -- registration -----------------------------------------------------------------

    api.on("session_start", cast(ExtensionHandler, start))
    api.on("before_agent_start", cast(ExtensionHandler, before_agent_start))
    api.on("turn_start", cast(ExtensionHandler, turn_start))
    api.on("input", cast(ExtensionHandler, on_input))
    api.on("tool_execution_start", cast(ExtensionHandler, tool_start))
    api.on("tool_execution_end", cast(ExtensionHandler, tool_end))
    api.on("agent_settled", cast(ExtensionHandler, settled))
    api.register_task_handler("experience-review", coordinator.consume)
    register_store_commands(api, stores)
    register_learning_commands(
        api,
        config=config,
        stores=stores,
        curator=curator,
        coordinator=coordinator,
        ask=ask,
    )
    register_tools(api, stores, config)
    api.add_prompt_guideline(PROMPT_GUIDELINE)


class _ConfigurableCoordinator(ReviewCoordinator):
    """A coordinator whose policy is finalised once the session's environment is read."""

    def configure(self, cfg: ExperienceConfig) -> None:
        policy = ReviewPolicy(
            cooldown_seconds=cfg.review_cooldown_seconds,
            review_every_turns=cfg.review_every_turns,
            review_on_signals=cfg.review_on_signals,
            max_iterations=cfg.review_max_iterations,
            max_input_tokens=cfg.review_max_input_tokens,
            thinking_level=cfg.review_thinking,
            max_output_tokens=cfg.review_max_output_tokens,
            cancel_timeout_seconds=cfg.review_cancel_timeout_seconds,
        )
        from .review import ReviewTrigger

        self._policy = policy
        self._trigger = ReviewTrigger(policy=policy)
        self._enabled = cfg.review_enabled
        self._notify = cfg.review_notify
