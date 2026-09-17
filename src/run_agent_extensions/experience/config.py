"""Experience configuration read from the environment, the way the gateway does it.

Defaults follow hermes-agent's ``memory``, ``skills``, ``auxiliary.background_review``
and ``curator`` config sections, so the extension behaves the same way out of the box.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from run_agent_coding.thinking import THINKING_LEVELS, ThinkingLevel, normalize_thinking_level

NotifyMode = Literal["off", "on", "verbose"]


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
    """Everything the extension reads from the environment at session start."""

    # -- memory (hermes ``memory.*``) -------------------------------------------------
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    memory_enabled: bool = True
    user_profile_enabled: bool = True
    # A review nudge every N user turns without a memory write (``memory.nudge_interval``).
    memory_nudge_interval: int = 10
    # Stage memory writes for approval instead of committing them (``memory.write_approval``).
    memory_write_approval: bool = False
    # Notification detail for background writes (``display.memory_notifications``).
    memory_notifications: NotifyMode = "on"

    # -- skills (hermes ``skills.*``) -------------------------------------------------
    # A skill-review nudge every N model rounds without a skill_manage call
    # (``skills.creation_nudge_interval``).
    skill_nudge_interval: int = 10
    # Security scan of skills the agent writes (``skills.guard_agent_created``).
    skill_guard: bool = False
    # Audit ledger of every skill mutation (``skills.ledger``).
    skill_ledger: bool = True
    # Stage skill writes for approval (``skills.write_approval``).
    skills_write_approval: bool = False

    # -- background review (hermes ``auxiliary.background_review.*``) -----------------
    review_enabled: bool = True
    review_max_iterations: int = 16
    review_max_input_tokens: int = 600_000
    review_thinking: ThinkingLevel = "off"
    review_max_output_tokens: int = 1600
    review_cancel_timeout_seconds: float = 2.0
    # Runs that ended in failure or a user correction also admit a review; hermes reviews
    # on the nudge cadence only, so this stays off unless the host opts in.
    review_on_signals: bool = False
    review_cooldown_seconds: float = 0.0
    review_notify: NotifyMode = "on"

    # -- curator (hermes ``curator.*``) ------------------------------------------------
    curator_enabled: bool = True
    curator_interval_hours: float = 168.0
    curator_min_idle_hours: float = 2.0
    curator_stale_after_days: int = 30
    curator_archive_after_days: int = 90
    curator_consolidate: bool = False
    curator_max_iterations: int = 9999
    # Whole-library snapshots before a mutating curator run (``curator.backup.*``).
    curator_backup: bool = True
    curator_backup_keep: int = 5

    @property
    def review_every_turns(self) -> int:
        """Kept for callers that still read the old name."""
        return self.memory_nudge_interval

    @property
    def curator_min_idle_seconds(self) -> float:
        return self.curator_min_idle_hours * 3600.0

    def __post_init__(self) -> None:
        if self.review_thinking not in THINKING_LEVELS:
            raise ValueError(f"Unknown review thinking level: {self.review_thinking!r}")
        if type(self.review_max_output_tokens) is not int or self.review_max_output_tokens < 1:
            raise ValueError("Review output token ceiling must be a positive integer")
        if self.memory_char_limit < 100 or self.user_char_limit < 100:
            raise ValueError("Memory character limits must be at least 100")
        for mode in (self.review_notify, self.memory_notifications):
            if mode not in {"off", "on", "verbose"}:
                raise ValueError(f"Unknown notify mode: {mode}")
        if self.curator_archive_after_days < self.curator_stale_after_days:
            raise ValueError("Archive threshold must not be shorter than the stale threshold")
        if self.review_max_iterations < 1 or self.curator_max_iterations < 1:
            raise ValueError("Iteration ceilings must be positive")


def _notify(env: Mapping[str, str], key: str) -> NotifyMode:
    text = (env.get(key) or "on").strip().lower()
    if text not in {"off", "on", "verbose"}:
        raise ValueError(f"Unknown {key}: {text!r}")
    return "off" if text == "off" else ("verbose" if text == "verbose" else "on")


def load_experience_config(env: Mapping[str, str]) -> ExperienceConfig:
    return ExperienceConfig(
        memory_char_limit=_int(env.get("EXPERIENCE_MEMORY_CHAR_LIMIT"), 2200, minimum=100),
        user_char_limit=_int(env.get("EXPERIENCE_USER_CHAR_LIMIT"), 1375, minimum=100),
        memory_enabled=_bool(env.get("EXPERIENCE_MEMORY_ENABLED"), True),
        user_profile_enabled=_bool(env.get("EXPERIENCE_USER_PROFILE_ENABLED"), True),
        memory_nudge_interval=_int(
            env.get("EXPERIENCE_MEMORY_NUDGE_INTERVAL") or env.get("EXPERIENCE_REVIEW_EVERY_TURNS"),
            10,
            minimum=0,
        ),
        memory_write_approval=_bool(env.get("EXPERIENCE_MEMORY_WRITE_APPROVAL"), False),
        memory_notifications=_notify(env, "EXPERIENCE_MEMORY_NOTIFICATIONS"),
        skill_nudge_interval=_int(env.get("EXPERIENCE_SKILL_NUDGE_INTERVAL"), 10, minimum=0),
        skill_guard=_bool(env.get("EXPERIENCE_SKILL_GUARD"), False),
        skill_ledger=_bool(env.get("EXPERIENCE_SKILL_LEDGER"), True),
        skills_write_approval=_bool(env.get("EXPERIENCE_SKILLS_WRITE_APPROVAL"), False),
        review_enabled=_bool(env.get("EXPERIENCE_REVIEW_ENABLED"), True),
        review_thinking=normalize_thinking_level(env.get("EXPERIENCE_REVIEW_THINKING") or "off"),
        review_max_output_tokens=_int(
            env.get("EXPERIENCE_REVIEW_MAX_OUTPUT_TOKENS"), 1600, minimum=1
        ),
        review_max_iterations=_int(env.get("EXPERIENCE_REVIEW_MAX_ITERATIONS"), 16, minimum=1),
        review_max_input_tokens=_int(
            env.get("EXPERIENCE_REVIEW_MAX_INPUT_TOKENS"), 600_000, minimum=0
        ),
        review_cancel_timeout_seconds=_float(
            env.get("EXPERIENCE_REVIEW_CANCEL_TIMEOUT_SECONDS"), 2.0, minimum=0.0
        ),
        review_on_signals=_bool(env.get("EXPERIENCE_REVIEW_ON_SIGNALS"), False),
        review_cooldown_seconds=_float(
            env.get("EXPERIENCE_REVIEW_COOLDOWN_SECONDS"), 0.0, minimum=0.0
        ),
        review_notify=_notify(env, "EXPERIENCE_REVIEW_NOTIFY"),
        curator_enabled=_bool(env.get("EXPERIENCE_CURATOR_ENABLED"), True),
        curator_interval_hours=_float(
            env.get("EXPERIENCE_CURATOR_INTERVAL_HOURS"), 168.0, minimum=0.01
        ),
        curator_min_idle_hours=_float(
            env.get("EXPERIENCE_CURATOR_MIN_IDLE_HOURS"), 2.0, minimum=0.0
        ),
        curator_stale_after_days=_int(env.get("EXPERIENCE_CURATOR_STALE_DAYS"), 30, minimum=1),
        curator_archive_after_days=_int(env.get("EXPERIENCE_CURATOR_ARCHIVE_DAYS"), 90, minimum=1),
        curator_consolidate=_bool(env.get("EXPERIENCE_CURATOR_CONSOLIDATE"), False),
        curator_max_iterations=_int(env.get("EXPERIENCE_CURATOR_MAX_ITERATIONS"), 9999, minimum=1),
        curator_backup=_bool(env.get("EXPERIENCE_CURATOR_BACKUP"), True),
        curator_backup_keep=_int(env.get("EXPERIENCE_CURATOR_BACKUP_KEEP"), 5, minimum=1),
    )


__all__ = ["ExperienceConfig", "NotifyMode", "load_experience_config"]
