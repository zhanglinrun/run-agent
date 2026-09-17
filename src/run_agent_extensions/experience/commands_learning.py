"""The ``/review`` and ``/curator`` commands: the learning machinery's controls."""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from typing import Any, cast

from run_agent_coding.extensions import ExtensionAPI, ExtensionCommandContext
from run_agent_coding.host.learning import LearningWritebackDisabled

from .config import ExperienceConfig
from .curator import Curator
from .memory import MemoryScope
from .mutation import MutationRejected, require_mutation
from .review import ReviewCoordinator
from .stores import ExperienceStores
from .tools import scope_of
from .write_approval import approve_write

REVIEW_USAGE = "/review now [focus]; /review status"
CURATOR_USAGE = (
    "/curator status|run|dry-run|pause|resume; "
    "/curator pin|unpin|adopt|restore <name>; /curator ledger [name]; "
    "/curator rollback <entry-id>; /curator archived [--scope project|user]"
)

Ask = Callable[[str, str], Awaitable[str]]


def register_learning_commands(
    api: ExtensionAPI,
    *,
    config: Callable[[], ExperienceConfig],
    stores: Callable[[], ExperienceStores],
    curator: Callable[[], Curator],
    coordinator: ReviewCoordinator,
    ask: Ask,
) -> None:
    async def review_command(args: str, context: ExtensionCommandContext) -> str:
        del context
        words = args.split(maxsplit=1)
        if not words:
            return REVIEW_USAGE
        if words[0] == "now":
            return await coordinator.request_now(words[1] if len(words) > 1 else "")
        if words[0] == "status":
            outcome = coordinator.last_outcome
            cfg = config()
            cadence = f"every {cfg.review_every_turns} runs"
            if cfg.review_on_signals:
                cadence += " or on failure/correction"
            head = (
                f"review: {'enabled' if cfg.review_enabled else 'disabled'}; {cadence}; "
                f"cooldown {cfg.review_cooldown_seconds:g}s; notify={cfg.review_notify}"
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
        words = shlex.split(args)
        if not words:
            return curator().status_text()
        scope: MemoryScope = "project"
        if "--scope" in words:
            index = words.index("--scope")
            if index + 1 >= len(words) or words[index + 1] not in {"project", "user"}:
                raise ValueError("--scope needs project or user")
            scope = cast(MemoryScope, words[index + 1])
            del words[index : index + 2]
        if not words:
            return CURATOR_USAGE
        action, *parts = words
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

        resolved = stores()
        manager = resolved.skills
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
            return CURATOR_USAGE
        name = parts[0]
        scope = scope_of(resolved, name, scope)
        if action in {"pin", "unpin", "adopt", "restore", "rollback"}:
            if resolved.config.skills_write_approval:
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
            return current.pin(scope, name, action == "pin")
        if action == "adopt":
            return current.adopt(scope, name)
        if action == "restore":
            return current.restore(scope, name)
        if action == "rollback":
            with manager.write_scope(scope):
                ok, message = manager.ledger[scope].rollback(name)
            return message if ok else f"Refused: {message}"
        return CURATOR_USAGE

    api.register_command(
        "review", review_command, description="Run or inspect the background learning review."
    )
    api.register_command(
        "curator",
        curator_command,
        description="Age, archive, pin, adopt, restore and roll back Skills.",
    )


__all__ = ["CURATOR_USAGE", "REVIEW_USAGE", "register_learning_commands"]
