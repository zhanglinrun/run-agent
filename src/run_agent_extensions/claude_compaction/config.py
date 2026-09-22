"""Environment configuration for the four-layer Claude compaction port.

Every knob is optional and prefixed ``COMPACTION_FOUR_LAYER_``, following the
naming style of ``run_agent_extensions.experience.config``. Defaults are the
reference implementation's constants (``autoCompact.ts``, ``timeBasedMCConfig.ts``,
``cachedMicrocompact.ts``, ``sessionMemoryCompact.ts``); the environment only
overrides them.

The context window is configured here rather than read from the provider: the
extension-facing API exposes no provider window, so a deployment that runs with a
different window sets ``COMPACTION_FOUR_LAYER_CONTEXT_WINDOW``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _bool(value: str | None, default: bool, *, name: str) -> bool:
    if value is None or not value.strip():
        return default
    text = value.strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} expects a boolean, got {value!r}")


def _int(value: str | None, default: int, *, name: str, minimum: int = 0) -> int:
    if value is None or not value.strip():
        return default
    number = int(value)
    if number < minimum:
        raise ValueError(f"{name} expects an integer >= {minimum}, got {value!r}")
    return number


def _float(value: str | None, default: float, *, name: str, minimum: float) -> float:
    if value is None or not value.strip():
        return default
    number = float(value)
    if number < minimum:
        raise ValueError(f"{name} expects a number >= {minimum}, got {value!r}")
    return number


def _percent(value: str | None, *, name: str) -> float | None:
    if value is None or not value.strip():
        return None
    number = float(value)
    if not 0 < number <= 100:
        raise ValueError(f"{name} expects a percentage in (0, 100], got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class FourLayerConfig:
    """Resolved configuration for all four layers."""

    enabled: bool = True
    l1_enabled: bool = True
    l2_enabled: bool = True
    l3_enabled: bool = True
    l4_enabled: bool = True
    reactive_enabled: bool = True
    # L1: keep this many most-recent compactable tool results (keepRecent).
    keep_recent: int = 5
    # cachedMicrocompact: trigger above this many live tool results, keep KEEP_RECENT.
    cached_trigger_threshold: int = 10
    # timeBasedMCConfig: 60 minutes is the server's guaranteed-expired cache TTL.
    time_based_enabled: bool = True
    time_gap_threshold_minutes: int = 60
    # snipCompact: message-count threshold for the model-facing nudge.
    snip_nudge_threshold: int = 30
    user_nudge: bool = True
    image_max_tokens: int = 2_000
    # autoCompact: window arithmetic.
    context_window: int = 128_000
    max_output_tokens_for_summary: int = 20_000
    keep_recent_tokens: int = 20_000
    autocompact_buffer_tokens: int = 13_000
    autocompact_pct_override: float | None = None
    max_consecutive_failures: int = 3
    summary_timeout_seconds: float = 60.0
    inline_model_attempt: bool = True
    # sessionMemoryCompact defaults (DEFAULT_SM_COMPACT_CONFIG).
    sm_min_tokens: int = 10_000
    sm_min_text_block_messages: int = 5
    sm_max_tokens: int = 40_000

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("The context window must be positive")
        if self.max_output_tokens_for_summary <= 0:
            raise ValueError("The summary output budget must be positive")
        if self.keep_recent < 1 or self.cached_trigger_threshold < 1:
            raise ValueError("L1 keep-recent and trigger thresholds must be at least 1")
        if self.time_gap_threshold_minutes < 0:
            raise ValueError("The time-based gap threshold must not be negative")
        if self.snip_nudge_threshold < 1:
            raise ValueError("The snip nudge threshold must be at least 1")
        if self.max_consecutive_failures < 1:
            raise ValueError("The autocompact failure limit must be at least 1")
        if self.summary_timeout_seconds <= 0:
            raise ValueError("The summary timeout must be positive")
        if self.sm_min_tokens < 0 or self.sm_max_tokens < self.sm_min_tokens:
            raise ValueError("Session-memory token bounds must satisfy 0 <= min <= max")


def load_config(environment: Mapping[str, str]) -> FourLayerConfig:
    """Build a :class:`FourLayerConfig` from an environment mapping."""
    return FourLayerConfig(
        enabled=_bool(environment.get("COMPACTION_FOUR_LAYER_ENABLED"), True, name="ENABLED"),
        l1_enabled=_bool(environment.get("COMPACTION_FOUR_LAYER_L1_ENABLED"), True, name="L1"),
        l2_enabled=_bool(environment.get("COMPACTION_FOUR_LAYER_L2_ENABLED"), True, name="L2"),
        l3_enabled=_bool(environment.get("COMPACTION_FOUR_LAYER_L3_ENABLED"), True, name="L3"),
        l4_enabled=_bool(environment.get("COMPACTION_FOUR_LAYER_L4_ENABLED"), True, name="L4"),
        reactive_enabled=_bool(
            environment.get("COMPACTION_FOUR_LAYER_REACTIVE_ENABLED"), True, name="REACTIVE"
        ),
        keep_recent=_int(
            environment.get("COMPACTION_FOUR_LAYER_KEEP_RECENT"),
            5,
            name="KEEP_RECENT",
            minimum=1,
        ),
        cached_trigger_threshold=_int(
            environment.get("COMPACTION_FOUR_LAYER_CACHED_TRIGGER_THRESHOLD"),
            10,
            name="CACHED_TRIGGER_THRESHOLD",
            minimum=1,
        ),
        time_based_enabled=_bool(
            environment.get("COMPACTION_FOUR_LAYER_TIME_BASED_ENABLED"), True, name="TIME_BASED"
        ),
        time_gap_threshold_minutes=_int(
            environment.get("COMPACTION_FOUR_LAYER_TIME_GAP_MINUTES"),
            60,
            name="TIME_GAP_MINUTES",
            minimum=0,
        ),
        snip_nudge_threshold=_int(
            environment.get("COMPACTION_FOUR_LAYER_SNIP_NUDGE_THRESHOLD"),
            30,
            name="SNIP_NUDGE_THRESHOLD",
            minimum=1,
        ),
        user_nudge=_bool(
            environment.get("COMPACTION_FOUR_LAYER_USER_NUDGE"), True, name="USER_NUDGE"
        ),
        image_max_tokens=_int(
            environment.get("COMPACTION_FOUR_LAYER_IMAGE_MAX_TOKENS"),
            2_000,
            name="IMAGE_MAX_TOKENS",
            minimum=1,
        ),
        context_window=_int(
            environment.get("COMPACTION_FOUR_LAYER_CONTEXT_WINDOW"),
            128_000,
            name="CONTEXT_WINDOW",
            minimum=1,
        ),
        max_output_tokens_for_summary=_int(
            environment.get("COMPACTION_FOUR_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY"),
            20_000,
            name="MAX_OUTPUT_TOKENS_FOR_SUMMARY",
            minimum=1,
        ),
        keep_recent_tokens=_int(
            environment.get("COMPACTION_FOUR_LAYER_KEEP_RECENT_TOKENS"),
            20_000,
            name="KEEP_RECENT_TOKENS",
            minimum=0,
        ),
        autocompact_buffer_tokens=_int(
            environment.get("COMPACTION_FOUR_LAYER_AUTOCOMPACT_BUFFER_TOKENS"),
            13_000,
            name="AUTOCOMPACT_BUFFER_TOKENS",
            minimum=0,
        ),
        autocompact_pct_override=_percent(
            environment.get("COMPACTION_FOUR_LAYER_AUTOCOMPACT_PCT_OVERRIDE"),
            name="AUTOCOMPACT_PCT_OVERRIDE",
        ),
        max_consecutive_failures=_int(
            environment.get("COMPACTION_FOUR_LAYER_MAX_CONSECUTIVE_FAILURES"),
            3,
            name="MAX_CONSECUTIVE_FAILURES",
            minimum=1,
        ),
        summary_timeout_seconds=_float(
            environment.get("COMPACTION_FOUR_LAYER_SUMMARY_TIMEOUT_SECONDS"),
            60.0,
            name="SUMMARY_TIMEOUT_SECONDS",
            minimum=0.001,
        ),
        inline_model_attempt=_bool(
            environment.get("COMPACTION_FOUR_LAYER_INLINE_MODEL_ATTEMPT"),
            True,
            name="INLINE_MODEL_ATTEMPT",
        ),
        sm_min_tokens=_int(
            environment.get("COMPACTION_FOUR_LAYER_SM_MIN_TOKENS"),
            10_000,
            name="SM_MIN_TOKENS",
            minimum=0,
        ),
        sm_min_text_block_messages=_int(
            environment.get("COMPACTION_FOUR_LAYER_SM_MIN_TEXT_BLOCK_MESSAGES"),
            5,
            name="SM_MIN_TEXT_BLOCK_MESSAGES",
            minimum=0,
        ),
        sm_max_tokens=_int(
            environment.get("COMPACTION_FOUR_LAYER_SM_MAX_TOKENS"),
            40_000,
            name="SM_MAX_TOKENS",
            minimum=0,
        ),
    )


__all__ = ["FourLayerConfig", "load_config"]
