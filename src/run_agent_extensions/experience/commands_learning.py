"""The ``/learn``, ``/review`` and ``/curator`` commands: the learning machinery's controls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from run_agent_coding.extensions import ExtensionAPI, ExtensionCommandContext

from .config import ExperienceConfig
from .curator import Curator
from .learn import build_learn_prompt
from .review import ReviewCoordinator

REVIEW_USAGE = "/review now [focus]; /review status"
CURATOR_USAGE = "/curator status|run|dry-run|pause|resume"

Ask = Callable[[str, str], Awaitable[str]]


def register_learning_commands(
    api: ExtensionAPI,
    *,
    config: Callable[[], ExperienceConfig],
    curator: Callable[[], Curator],
    coordinator: ReviewCoordinator,
    ask: Ask,
) -> None:
    async def learn_command(args: str, context: ExtensionCommandContext) -> str:
        api.send_user_message(build_learn_prompt(args))
        return "Learning a skill from your request…"

    async def review_command(args: str, context: ExtensionCommandContext) -> str:
        words = args.split(maxsplit=1)
        if not words:
            return REVIEW_USAGE
        if words[0] == "now":
            return await coordinator.request_now(words[1] if len(words) > 1 else "")
        if words[0] == "status":
            outcome = coordinator.last_outcome
            cfg = config()
            head = (
                f"review: {'enabled' if cfg.review_enabled else 'disabled'}; every "
                f"{cfg.review_every_turns} runs or on failure/correction; cooldown "
                f"{cfg.review_cooldown_seconds:g}s; notify={cfg.review_notify}"
            )
            if outcome is None:
                return f"{head}\nno review has completed in this session"
            applied = outcome.get("applied") or []
            skipped = outcome.get("skipped") or []
            error = outcome.get("error")
            counts = (
                f"{len(cast(list[Any], applied))} applied, {len(cast(list[Any], skipped))} refused"
            )
            status = outcome.get("status", "unknown")
            lines = [head, f"last review of {outcome.get('consumed')}: {status}; {counts}"]
            if outcome.get("stop_reason") or outcome.get("reason"):
                lines.append(f"reason: {outcome.get('stop_reason') or outcome.get('reason')}")
            if error:
                lines.append(f"error: {error}")
            return "\n".join(lines)
        return REVIEW_USAGE

    async def curator_command(args: str, context: ExtensionCommandContext) -> str:
        action = args.strip().split(maxsplit=1)[0] if args.strip() else "status"
        current = curator()
        if action == "status":
            return current.status_text()
        if action in {"run", "dry-run"}:
            available = api.context.services.inference.available
            run = await current.run(
                ask if available else None,
                dry_run=action == "dry-run",
                consolidate=True if action == "run" and available else None,
            )
            return f"{run.summary}" + (f"\nreport: {run.report_path}" if run.report_path else "")
        if action == "pause":
            current.set_paused(True)
            return "curator paused"
        if action == "resume":
            current.set_paused(False)
            return "curator resumed"
        return CURATOR_USAGE

    api.register_command(
        "learn", learn_command, description="Author a reusable Skill from sources you describe."
    )
    api.register_command(
        "review", review_command, description="Run or inspect the background learning review."
    )
    api.register_command(
        "curator", curator_command, description="Age, archive and consolidate agent-created Skills."
    )


__all__ = ["CURATOR_USAGE", "REVIEW_USAGE", "register_learning_commands"]
