"""Resolved memory, formal Skills and isolated candidate storage for one session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths

from .candidates import SkillCandidateStore
from .config import ExperienceConfig
from .memory import MemoryScope, MemoryStore, MemoryTarget, format_memory_context
from .skill_manager import SkillManager, SkillRoots


@dataclass(frozen=True, slots=True)
class ExperienceStores:
    memory: dict[MemoryScope, MemoryStore]
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
        limits: dict[MemoryTarget, int] = {
            "memory": chosen.memory_char_limit,
            "user": chosen.user_char_limit,
        }
        stores = cls(
            memory={
                "user": MemoryStore(paths.home, limits),
                "project": MemoryStore(paths.project_run_agent_dir(cwd), limits),
            },
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
        for scope, store in stores.memory.items():
            if scope == "project" and not project_enabled:
                continue
            store.load()
        return stores

    def target_enabled(self, target: MemoryTarget) -> bool:
        return self.config.user_profile_enabled if target == "user" else self.config.memory_enabled

    def snapshot(self) -> dict[MemoryScope, dict[MemoryTarget, str]]:
        result: dict[MemoryScope, dict[MemoryTarget, str]] = {}
        for scope, store in self.memory.items():
            if scope == "project" and not self.project_enabled:
                continue
            blocks = store.snapshot()
            result[scope] = {
                target: text for target, text in blocks.items() if self.target_enabled(target)
            }
        return result

    def prompt_block(self) -> str | None:
        return format_memory_context(self.snapshot())

    def reset_turn(self) -> None:
        for store in self.memory.values():
            store.reset_turn()

    def scope_for(self, target: MemoryTarget, scope: MemoryScope | None) -> MemoryScope:
        chosen = scope if scope is not None else ("user" if target == "user" else "project")
        self.require_scope(chosen)
        return chosen

    def require_scope(self, scope: MemoryScope) -> None:
        if scope == "project" and not self.project_enabled:
            raise ValueError(
                "project inputs are untrusted in this session; use the user scope or trust "
                "the project first"
            )
