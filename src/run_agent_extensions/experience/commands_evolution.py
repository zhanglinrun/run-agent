"""User controls for verifier-gated Skill evolution."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import cast

from run_agent_coding.extensions import ExtensionAPI, ExtensionCommandContext
from run_agent_coding.host.learning import LearningWritebackDisabled

from .candidates import CandidateError, CandidateStatus
from .evolution import SkillEvolution
from .memory import MemoryScope
from .mutation import MutationRejected, require_mutation
from .skill_manager import SkillWriteError
from .write_approval import approve_write

EVOLVE_USAGE = (
    "/evolve status|candidates [cold|verified|published|rejected|superseded]; "
    "/evolve show <candidate-id>; /evolve adopt <name> [--scope project|user]; "
    "/evolve publish <candidate-id>; /evolve reject <candidate-id> [reason]; "
    "/evolve ledger [name] [--scope project|user]; "
    "/evolve rollback <ledger-id> [--scope project|user]"
)


def register_evolution_commands(
    api: ExtensionAPI,
    evolution: Callable[[], SkillEvolution],
) -> None:
    async def evolve_command(args: str, context: ExtensionCommandContext) -> str:
        try:
            words = shlex.split(args)
        except ValueError as exc:
            return f"Refused: {exc}"
        if not words:
            return EVOLVE_USAGE
        try:
            scope, explicit_scope = _scope(words)
        except ValueError as exc:
            return f"Refused: {exc}"
        action, *parts = words
        current = evolution()
        try:
            if action == "status":
                return current.status_text()
            if action == "candidates":
                status: CandidateStatus | None = None
                if parts:
                    if parts[0] not in {
                        "cold",
                        "verified",
                        "published",
                        "rejected",
                        "superseded",
                    }:
                        return EVOLVE_USAGE
                    status = cast(CandidateStatus, parts[0])
                candidates = current.candidates.list(status=status)
                return (
                    "\n".join(
                        f"{item.candidate_id}  {item.status:<10} {item.scope}/{item.name} "
                        f"{item.candidate_digest[:12]} report={item.report_id or '-'}"
                        for item in candidates
                    )
                    or "No candidates."
                )
            if action == "show" and len(parts) == 1:
                candidate = current.candidates.require(parts[0])
                record = {
                    "candidate_id": candidate.candidate_id,
                    "status": candidate.status,
                    "scope": candidate.scope,
                    "name": candidate.name,
                    "source_session": candidate.source_session,
                    "source_run": candidate.source_run,
                    "base_digest": candidate.base_digest,
                    "candidate_digest": candidate.candidate_digest,
                    "report_id": candidate.report_id,
                    "operations": [
                        {
                            "action": operation.action,
                            "old_text": operation.old_text,
                            "new_text": operation.new_text,
                        }
                        for operation in candidate.operations
                    ],
                    "claims": [
                        {
                            "text": claim.text,
                            "probes": [
                                {"path": evidence.path, "sha256": evidence.sha256}
                                for evidence in claim.probes
                            ],
                        }
                        for claim in candidate.claims
                    ],
                }
                return (
                    json.dumps(record, indent=2, sort_keys=True)
                    + "\n\n"
                    + current.candidates.content(candidate)
                )
            if action == "ledger":
                entries = current.skills.ledger[scope].entries(
                    skill=parts[0] if parts else None, limit=20
                )
                return (
                    "\n".join(
                        f"{entry.id}  {entry.timestamp[:19]}  {entry.actor:<9} "
                        f"{entry.action:<10} {entry.skill}"
                        for entry in entries
                    )
                    or "The ledger is empty."
                )
            if action == "adopt" and len(parts) == 1:
                await _approve_formal_write(context, current, f"Adopt {scope}/{parts[0]}?")
                require_mutation("skill")
                return current.adopt(scope, parts[0]).message
            if action == "publish" and len(parts) == 1:
                await _approve_formal_write(context, current, f"Publish candidate {parts[0]}?")
                require_mutation("skill")
                return (await current.publish(parts[0])).message
            if action == "reject" and parts:
                require_mutation("candidate")
                reason = " ".join(parts[1:]) if len(parts) > 1 else "rejected by user"
                candidate = current.reject(parts[0], reason)
                return f"Rejected candidate {candidate.candidate_id}: {reason}"
            if action == "rollback" and len(parts) == 1:
                await _approve_formal_write(context, current, f"Rollback ledger entry {parts[0]}?")
                require_mutation("skill")
                chosen = scope
                if not explicit_scope:
                    found = [
                        candidate_scope
                        for candidate_scope in ("project", "user")
                        if current.skills.ledger[candidate_scope].get(parts[0]) is not None
                    ]
                    if len(found) != 1:
                        raise CandidateError(
                            "ledger entry was not found uniquely; pass --scope project|user"
                        )
                    chosen = cast(MemoryScope, found[0])
                with current.skills.write_scope(chosen):
                    ok, message = current.skills.ledger[chosen].rollback(parts[0])
                return message if ok else f"Refused: {message}"
        except (
            CandidateError,
            SkillWriteError,
            LearningWritebackDisabled,
            MutationRejected,
        ) as exc:
            return f"Refused: {exc}"
        return EVOLVE_USAGE

    api.register_command(
        "evolve",
        evolve_command,
        description="Inspect, adopt, publish, reject or roll back verified Skill candidates.",
    )


async def _approve_formal_write(
    context: ExtensionCommandContext, evolution: SkillEvolution, message: str
) -> None:
    approved = await approve_write(
        required=evolution.config.skills_write_approval,
        has_ui=context.api.context.has_ui,
        confirm=context.api.context.ui.confirm,
        title="Approve Skill evolution",
        message=message,
    )
    if not approved:
        raise CandidateError("Skill write was not approved")


def _scope(words: list[str]) -> tuple[MemoryScope, bool]:
    scope: MemoryScope = "project"
    explicit = False
    if "--scope" in words:
        index = words.index("--scope")
        if index + 1 >= len(words) or words[index + 1] not in {"project", "user"}:
            raise ValueError("--scope needs project or user")
        scope = cast(MemoryScope, words[index + 1])
        explicit = True
        del words[index : index + 2]
    return scope, explicit


__all__ = ["EVOLVE_USAGE", "register_evolution_commands"]
