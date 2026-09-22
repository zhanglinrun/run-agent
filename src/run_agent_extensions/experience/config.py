"""Configuration for verifier-gated Skill evolution."""

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


def _float(value: str | None, default: float, *, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if number < minimum:
        raise ValueError(f"Expected a number >= {minimum}, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class ExperienceConfig:
    skills_write_approval: bool = False
    skill_guard: bool = True
    skill_ledger: bool = True
    evolution_suite: str = "evolution"
    evolution_suite_version: str = "2"
    evolution_budget_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.evolution_suite.strip() or not self.evolution_suite_version.strip():
            raise ValueError("Evolution suite and version must not be empty")
        if self.evolution_budget_seconds <= 0:
            raise ValueError("Evolution evaluation budget must be positive")


def load_experience_config(env: Mapping[str, str]) -> ExperienceConfig:
    """Read the Skill-evolution settings; memory has its own ``HERMES_MEMORY_*`` set."""
    return ExperienceConfig(
        skills_write_approval=_bool(env.get("EXPERIENCE_SKILLS_WRITE_APPROVAL"), False),
        skill_guard=_bool(env.get("EXPERIENCE_SKILL_GUARD"), True),
        skill_ledger=_bool(env.get("EXPERIENCE_SKILL_LEDGER"), True),
        evolution_suite=(env.get("EXPERIENCE_EVOLUTION_SUITE") or "evolution").strip(),
        evolution_suite_version=(env.get("EXPERIENCE_EVOLUTION_SUITE_VERSION") or "2").strip(),
        evolution_budget_seconds=_float(
            env.get("EXPERIENCE_EVOLUTION_BUDGET_SECONDS"), 300.0, minimum=0.001
        ),
    )


__all__ = ["ExperienceConfig", "load_experience_config"]
