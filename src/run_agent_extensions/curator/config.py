"""Configuration for the Curator maintenance extension.

Every knob is an environment variable read from the extension context, so a session
cannot change the policy of a Curator pass it did not start. The defaults are the
conservative ones: automatic maintenance is on, but every *destructive* automatic
action is gated off for anything a user wrote and for project-scoped Skills.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_INTERVAL_HOURS = 168.0
DEFAULT_STALE_AFTER_DAYS = 30.0
DEFAULT_ARCHIVE_AFTER_DAYS = 90.0
DEFAULT_BACKUP_KEEP = 5
DEFAULT_LLM_REVIEW_MAX_OUTPUT_TOKENS = 1_200
DEFAULT_LLM_REVIEW_TIMEOUT_SECONDS = 60.0
DEFAULT_USAGE_LOOKBACK_DAYS = 30.0
MAX_BACKUP_KEEP = 100
MAX_LLM_REVIEW_MAX_OUTPUT_TOKENS = 8_000
MAX_LLM_REVIEW_TIMEOUT_SECONDS = 600.0
MAX_INTERVAL_HOURS = 24.0 * 365.0
MAX_AGE_DAYS = 3_650.0


def _bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean, got {value!r}")


def _float(
    value: str | None,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Expected a number between {minimum} and {maximum}, got {value!r}")
    return number


def _int(value: str | None, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or not value.strip():
        return default
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"Expected an integer between {minimum} and {maximum}, got {value!r}")
    return number


def _names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated list into a stable, de-duplicated tuple."""
    if value is None or not value.strip():
        return ()
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


@dataclass(frozen=True, slots=True)
class CuratorConfig:
    """Resolved Curator policy for one session."""

    enabled: bool = True
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    stale_after_days: float = DEFAULT_STALE_AFTER_DAYS
    archive_after_days: float = DEFAULT_ARCHIVE_AFTER_DAYS
    auto_archive_user_skills: bool = False
    auto_archive_project_skills: bool = False
    backup_enabled: bool = True
    backup_keep: int = DEFAULT_BACKUP_KEEP
    llm_review_enabled: bool = True
    llm_review_max_output_tokens: int = DEFAULT_LLM_REVIEW_MAX_OUTPUT_TOKENS
    llm_review_timeout_seconds: float = DEFAULT_LLM_REVIEW_TIMEOUT_SECONDS
    usage_lookback_days: float = DEFAULT_USAGE_LOOKBACK_DAYS
    protected_skill_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 < self.interval_hours <= MAX_INTERVAL_HOURS:
            raise ValueError("CURATOR_INTERVAL_HOURS must be a positive number of hours")
        if not 0.0 < self.stale_after_days <= MAX_AGE_DAYS:
            raise ValueError("CURATOR_STALE_AFTER_DAYS must be a positive number of days")
        if not 0.0 < self.archive_after_days <= MAX_AGE_DAYS:
            raise ValueError("CURATOR_ARCHIVE_AFTER_DAYS must be a positive number of days")
        if self.archive_after_days < self.stale_after_days:
            raise ValueError("CURATOR_ARCHIVE_AFTER_DAYS must not be shorter than the stale window")
        if not 1 <= self.backup_keep <= MAX_BACKUP_KEEP:
            raise ValueError("CURATOR_BACKUP_KEEP must be between 1 and 100")
        if not 1 <= self.llm_review_max_output_tokens <= MAX_LLM_REVIEW_MAX_OUTPUT_TOKENS:
            raise ValueError("CURATOR_LLM_REVIEW_MAX_OUTPUT_TOKENS is out of range")
        if not 0.0 < self.llm_review_timeout_seconds <= MAX_LLM_REVIEW_TIMEOUT_SECONDS:
            raise ValueError("CURATOR_LLM_REVIEW_TIMEOUT_SECONDS is out of range")
        if not 0.0 < self.usage_lookback_days <= MAX_AGE_DAYS:
            raise ValueError("CURATOR_USAGE_LOOKBACK_DAYS must be a positive number of days")

    @property
    def protected_names(self) -> frozenset[str]:
        """Return the never-auto-archived Skill names as a set."""
        return frozenset(self.protected_skill_names)

    def as_json(self) -> dict[str, object]:
        """Return the effective policy for a run report."""
        return {
            "enabled": self.enabled,
            "interval_hours": self.interval_hours,
            "stale_after_days": self.stale_after_days,
            "archive_after_days": self.archive_after_days,
            "auto_archive_user_skills": self.auto_archive_user_skills,
            "auto_archive_project_skills": self.auto_archive_project_skills,
            "backup_enabled": self.backup_enabled,
            "backup_keep": self.backup_keep,
            "llm_review_enabled": self.llm_review_enabled,
            "llm_review_max_output_tokens": self.llm_review_max_output_tokens,
            "llm_review_timeout_seconds": self.llm_review_timeout_seconds,
            "usage_lookback_days": self.usage_lookback_days,
            "protected_skill_names": list(self.protected_skill_names),
        }


def load_curator_config(env: Mapping[str, str]) -> CuratorConfig:
    """Read the ``CURATOR_*`` settings, refusing an unusable combination."""
    return CuratorConfig(
        enabled=_bool(env.get("CURATOR_ENABLED"), True),
        interval_hours=_float(
            env.get("CURATOR_INTERVAL_HOURS"),
            DEFAULT_INTERVAL_HOURS,
            minimum=0.001,
            maximum=MAX_INTERVAL_HOURS,
        ),
        stale_after_days=_float(
            env.get("CURATOR_STALE_AFTER_DAYS"),
            DEFAULT_STALE_AFTER_DAYS,
            minimum=0.001,
            maximum=MAX_AGE_DAYS,
        ),
        archive_after_days=_float(
            env.get("CURATOR_ARCHIVE_AFTER_DAYS"),
            DEFAULT_ARCHIVE_AFTER_DAYS,
            minimum=0.001,
            maximum=MAX_AGE_DAYS,
        ),
        auto_archive_user_skills=_bool(env.get("CURATOR_AUTO_ARCHIVE_USER_SKILLS"), False),
        auto_archive_project_skills=_bool(env.get("CURATOR_AUTO_ARCHIVE_PROJECT_SKILLS"), False),
        backup_enabled=_bool(env.get("CURATOR_BACKUP_ENABLED"), True),
        backup_keep=_int(
            env.get("CURATOR_BACKUP_KEEP"),
            DEFAULT_BACKUP_KEEP,
            minimum=1,
            maximum=MAX_BACKUP_KEEP,
        ),
        llm_review_enabled=_bool(env.get("CURATOR_LLM_REVIEW_ENABLED"), True),
        llm_review_max_output_tokens=_int(
            env.get("CURATOR_LLM_REVIEW_MAX_OUTPUT_TOKENS"),
            DEFAULT_LLM_REVIEW_MAX_OUTPUT_TOKENS,
            minimum=1,
            maximum=MAX_LLM_REVIEW_MAX_OUTPUT_TOKENS,
        ),
        llm_review_timeout_seconds=_float(
            env.get("CURATOR_LLM_REVIEW_TIMEOUT_SECONDS"),
            DEFAULT_LLM_REVIEW_TIMEOUT_SECONDS,
            minimum=0.001,
            maximum=MAX_LLM_REVIEW_TIMEOUT_SECONDS,
        ),
        usage_lookback_days=_float(
            env.get("CURATOR_USAGE_LOOKBACK_DAYS"),
            DEFAULT_USAGE_LOOKBACK_DAYS,
            minimum=0.001,
            maximum=MAX_AGE_DAYS,
        ),
        protected_skill_names=_names(env.get("CURATOR_PROTECTED_SKILL_NAMES")),
    )


__all__ = [
    "DEFAULT_ARCHIVE_AFTER_DAYS",
    "DEFAULT_BACKUP_KEEP",
    "DEFAULT_INTERVAL_HOURS",
    "DEFAULT_LLM_REVIEW_MAX_OUTPUT_TOKENS",
    "DEFAULT_LLM_REVIEW_TIMEOUT_SECONDS",
    "DEFAULT_STALE_AFTER_DAYS",
    "DEFAULT_USAGE_LOOKBACK_DAYS",
    "CuratorConfig",
    "load_curator_config",
]
