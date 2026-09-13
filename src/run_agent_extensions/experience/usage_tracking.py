"""Attribute successful Skill consultations to their selected source library."""

from __future__ import annotations

from pathlib import Path

from run_agent_coding.host.learning import is_agent_created, writeback_enabled
from run_agent_coding.skills import Skill

from .skill_manager import SkillManager


def _same_package(frozen: Path, source: Path) -> bool:
    """A frozen old package must not count as reuse of a newer patch."""
    try:

        def files(root: Path) -> dict[Path, Path]:
            return {
                path.relative_to(root): path
                for path in root.rglob("*")
                if path.is_file()
                and not {".git", "__pycache__"}.intersection(path.relative_to(root).parts)
            }

        left, right = files(frozen), files(source)
        return left.keys() == right.keys() and all(
            path.read_bytes() == right[name].read_bytes() for name, path in left.items()
        )
    except OSError:
        return False


class SkillConsultations:
    """Views count successful reads; uses count at most once per Skill per run."""

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager
        self.used: set[tuple[str, str]] = set()
        self.reads: dict[str, tuple[Skill, Path]] = {}

    def begin(self) -> None:
        self.used.clear()
        self.reads.clear()

    def reading(self, call_id: str, path: Path, skills: tuple[Skill, ...]) -> None:
        path = path.resolve()
        for skill in skills:
            if path in {skill.path.resolve(), (skill.source_path or skill.path).resolve()}:
                self.reads[call_id] = (skill, path)
                break

    def read_finished(self, call_id: str, *, succeeded: bool) -> None:
        selected = self.reads.pop(call_id, None)
        if succeeded and selected is not None:
            self.record(*selected, viewed=True)

    def record(self, skill: Skill, path: Path, *, viewed: bool = False) -> None:
        if not writeback_enabled() or is_agent_created():
            return
        source = (skill.source_path or skill.path).resolve()
        for scope in ("user", "project"):
            directory = self.manager.find(scope, skill.name)
            if directory is None or (directory / "SKILL.md").resolve() != source:
                continue
            with self.manager.write_scope(scope):
                usage = self.manager.usage[scope]
                if viewed:
                    usage.bump_view(skill.name)
                key = (scope, skill.name)
                if key not in self.used:
                    usage.bump_use(
                        skill.name,
                        count_patch_reuse=path.resolve() == source
                        or _same_package(skill.path.parent, source.parent),
                    )
                    self.used.add(key)
            break
