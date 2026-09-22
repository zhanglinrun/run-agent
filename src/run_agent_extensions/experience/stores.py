"""Resolved formal Skills and isolated candidate storage for one session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths

from .candidates import SkillCandidateStore
from .config import ExperienceConfig
from .scopes import Scope
from .skill_manager import SkillManager, SkillRoots


@dataclass(frozen=True, slots=True)
class ExperienceStores:
    skills: SkillManager
    candidates: SkillCandidateStore
    config: ExperienceConfig
    project_enabled: bool = True

    @classmethod
    def resolve(
        cls,
        paths: RunAgentPaths,
        cwd: Path,
        *,
        config: ExperienceConfig | None = None,
        project_enabled: bool = True,
        session_id: str | None = None,
    ) -> ExperienceStores:
        chosen = config or ExperienceConfig()
        return cls(
            skills=SkillManager(
                SkillRoots(user=paths.user_skills_dir, project=paths.project_skills_dir(cwd)),
                guard=chosen.skill_guard,
                ledger=chosen.skill_ledger,
                session_id=session_id,
            ),
            # Deliberately outside every Skill discovery root.
            candidates=SkillCandidateStore(paths.home / "experience" / "candidates"),
            config=chosen,
            project_enabled=project_enabled,
        )

    def require_scope(self, scope: Scope) -> None:
        if scope == "project" and not self.project_enabled:
            raise ValueError(
                "project inputs are untrusted in this session; use the user scope or trust "
                "the project first"
            )


__all__ = ["ExperienceStores"]
