"""Configuration for memory and verifier-gated Skill evolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean, got {value!r}")


def _int(value: str | None, default: int, *, minimum: int) -> int:
    if value is None or not value.strip():
        return default
    number = int(value)
    if number < minimum:
        raise ValueError(f"Expected an integer >= {minimum}, got {value!r}")
    return number


def _float(value: str | None, default: float, *, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if number < minimum:
        raise ValueError(f"Expected a number >= {minimum}, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class ExperienceConfig:
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    memory_enabled: bool = True
    user_profile_enabled: bool = True
    memory_write_approval: bool = False
    skills_write_approval: bool = False
    skill_guard: bool = True
    skill_ledger: bool = True
    evolution_suite: str = "evolution"
    evolution_suite_version: str = "1"
    evolution_budget_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.memory_char_limit < 100 or self.user_char_limit < 100:
            raise ValueError("Memory character limits must be at least 100")
        if not self.evolution_suite.strip() or not self.evolution_suite_version.strip():
            raise ValueError("Evolution suite and version must not be empty")
        if self.evolution_budget_seconds <= 0:
            raise ValueError("Evolution evaluation budget must be positive")


def load_experience_config(env: Mapping[str, str]) -> ExperienceConfig:
    return ExperienceConfig(
        memory_char_limit=_int(env.get("EXPERIENCE_MEMORY_CHAR_LIMIT"), 2200, minimum=100),
        user_char_limit=_int(env.get("EXPERIENCE_USER_CHAR_LIMIT"), 1375, minimum=100),
        memory_enabled=_bool(env.get("EXPERIENCE_MEMORY_ENABLED"), True),
        user_profile_enabled=_bool(env.get("EXPERIENCE_USER_PROFILE_ENABLED"), True),
        memory_write_approval=_bool(env.get("EXPERIENCE_MEMORY_WRITE_APPROVAL"), False),
        skills_write_approval=_bool(env.get("EXPERIENCE_SKILLS_WRITE_APPROVAL"), False),
        skill_guard=_bool(env.get("EXPERIENCE_SKILL_GUARD"), True),
        skill_ledger=_bool(env.get("EXPERIENCE_SKILL_LEDGER"), True),
        evolution_suite=(env.get("EXPERIENCE_EVOLUTION_SUITE") or "evolution").strip(),
        evolution_suite_version=(env.get("EXPERIENCE_EVOLUTION_SUITE_VERSION") or "1").strip(),
        evolution_budget_seconds=_float(
            env.get("EXPERIENCE_EVOLUTION_BUDGET_SECONDS"), 300.0, minimum=0.001
        ),
    )


__all__ = ["ExperienceConfig", "load_experience_config"]
