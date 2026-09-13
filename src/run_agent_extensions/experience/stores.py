"""Where the Markdown memory and the managed Skills live for one session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths

from .config import ExperienceConfig
from .memory import MemoryScope, MemoryStore, MemoryTarget, format_memory_context
from .skill_manager import SkillManager, SkillRoots


@dataclass(frozen=True, slots=True)
class ExperienceStores:
    """Both memory scopes and the skill manager, resolved from the host paths.

    User-scope files sit next to the other user resources under the Run Agent home
    (``~/.run/USER.md``, ``~/.run/MEMORY.md``, ``~/.run/skills/``); project-scope files
    sit under the project's ``.run`` directory, which is where the skill loader already
    looks for project skills.
    """

    memory: dict[MemoryScope, MemoryStore]
    skills: SkillManager
    config: ExperienceConfig
    # Project-local files share the project trust gate with every other project input:
    # an untrusted working directory contributes nothing to the prompt and accepts no
    # writes, so a checkout cannot plant memory the agent then treats as its own.
    project_enabled: bool = True

    @classmethod
    def resolve(
        cls,
        paths: RunAgentPaths,
        cwd: Path,
        *,
        config: ExperienceConfig | None = None,
        project_enabled: bool = True,
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
            ),
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
            result[scope] = {t: text for t, text in blocks.items() if self.target_enabled(t)}
        return result

    def prompt_block(self) -> str | None:
        return format_memory_context(self.snapshot())

    def reset_turn(self) -> None:
        for store in self.memory.values():
            store.reset_turn()

    def scope_for(self, target: MemoryTarget, scope: MemoryScope | None) -> MemoryScope:
        """USER.md defaults to the user scope, MEMORY.md to the project."""
        chosen = scope if scope is not None else ("user" if target == "user" else "project")
        self.require_scope(chosen)
        return chosen

    def require_scope(self, scope: MemoryScope) -> None:
        if scope == "project" and not self.project_enabled:
            raise ValueError(
                "project inputs are untrusted in this session; use the user scope or trust "
                "the project first"
            )
