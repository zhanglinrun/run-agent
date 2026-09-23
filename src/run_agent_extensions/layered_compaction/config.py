"""Environment configuration for the cheap-first compaction extension.

Every knob is optional and prefixed ``COMPACTION_LAYER_``, following the naming
style of ``run_agent_extensions.experience.config``. Defaults are the reference
implementation's constants; the environment only overrides them.

The *budget* is the context window: ``COMPACTION_LAYER_CONTEXT_WINDOW`` is an
override, never a fallback, and the effective window is
``min(model window, override)`` — the same number the core's hard guard refuses
requests on. Two derived numbers follow from it:

* ``budget_threshold = (budget * 4) // 5``: below it the pipeline runs nothing
  at all, above it the three free layers run as a batch and only a view that is
  still over the threshold goes on to the paid summary layer;
* ``keep_recent_tokens`` defaults to ``budget // 4``: how much of the tail the
  summary layer keeps verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .layers import (
    DEFAULT_KEEP_RECENT_RESULTS,
    DEFAULT_PERSIST_THRESHOLD_CHARS,
    DEFAULT_PLACEHOLDER_MIN_CHARS,
    DEFAULT_SNIP_MAX_MESSAGES,
)

CONTEXT_WINDOW_ENV = "COMPACTION_LAYER_CONTEXT_WINDOW"
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000
BUDGET_THRESHOLD_PERCENT = 80

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


def budget_threshold(context_window: int) -> int:
    """Return the token count at which the pipeline starts working."""
    return (context_window * BUDGET_THRESHOLD_PERCENT) // 100


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """Resolved configuration for the four layers."""

    enabled: bool = True
    # L1 persist oversized tool results, L2 snip the middle of a long view,
    # L3 placeholder old tool results, L4 summarize what is left.
    l1_enabled: bool = True
    l2_enabled: bool = True
    l3_enabled: bool = True
    l4_enabled: bool = True
    reactive_enabled: bool = True
    # The budget, and the only source of it: the bound session's window, tightened
    # by an explicit override.
    context_window: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    # L4: how much of the tail the summary keeps verbatim (default: budget // 4).
    keep_recent_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS // 4
    # L1: tool results above this many characters are written to disk.
    persist_threshold_chars: int = DEFAULT_PERSIST_THRESHOLD_CHARS
    # L2: a view longer than this keeps a 3-message head and a placeholder.
    snip_max_messages: int = DEFAULT_SNIP_MAX_MESSAGES
    # L3: results at or below this many characters are never replaced.
    placeholder_min_chars: int = DEFAULT_PLACEHOLDER_MIN_CHARS
    # L3: the most recent results always survive.
    keep_recent_results: int = DEFAULT_KEEP_RECENT_RESULTS
    max_output_tokens_for_summary: int = 20_000
    max_consecutive_failures: int = 3
    summary_timeout_seconds: float = 60.0
    inline_model_attempt: bool = True

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("The context window must be positive")
        if self.keep_recent_tokens < 0:
            raise ValueError("The retained tail must not be negative")
        if self.persist_threshold_chars < 1:
            raise ValueError("The persist character threshold must be at least 1")
        if self.snip_max_messages < 1:
            raise ValueError("The snip message limit must be at least 1")
        if self.placeholder_min_chars < 0:
            raise ValueError("The placeholder character floor must not be negative")
        if self.keep_recent_results < 0:
            raise ValueError("The keep-recent result count must not be negative")
        if self.max_output_tokens_for_summary <= 0:
            raise ValueError("The summary output budget must be positive")
        if self.max_consecutive_failures < 1:
            raise ValueError("The failure limit must be at least 1")
        if self.summary_timeout_seconds <= 0:
            raise ValueError("The summary timeout must be positive")

    @property
    def budget_threshold(self) -> int:
        """Return the token count below which no layer runs."""
        return budget_threshold(self.context_window)


def should_compact(token_usage: int, config: CompactionConfig) -> bool:
    """Return whether a request view is over the gate and may be rewritten."""
    if not config.enabled:
        return False
    return token_usage > config.budget_threshold


def resolve_context_window(
    environment: Mapping[str, str], *, model_window: int | None
) -> int:
    """Return the budget the gate is computed from.

    ``COMPACTION_LAYER_CONTEXT_WINDOW`` is an override, never a fallback:
    unset (or blank) means the model's own window is authoritative, the same
    number the core's hard guard refuses requests on. When it is set explicitly
    the effective window is ``min(model_window, override)``, so the variable can
    only tighten the model window and the gate can never sit above the window
    the request is actually measured against. A model window that is unknown (no
    session bound) falls back to the override, then to the reference default.
    """
    raw = environment.get(CONTEXT_WINDOW_ENV)
    explicit: int | None = None
    if raw is not None and raw.strip():
        explicit = _int(raw, DEFAULT_CONTEXT_WINDOW_TOKENS, name="CONTEXT_WINDOW", minimum=1)
    if explicit is None:
        if model_window is None or model_window < 1:
            return DEFAULT_CONTEXT_WINDOW_TOKENS
        return model_window
    if model_window is None or model_window < 1:
        return explicit
    return min(model_window, explicit)


def load_config(
    environment: Mapping[str, str], *, model_window: int | None = None
) -> CompactionConfig:
    """Build a :class:`CompactionConfig` from an environment mapping.

    ``model_window`` is the bound session's ``context_window_tokens``; see
    :func:`resolve_context_window` for how the override combines with it. The
    retained-tail budget is derived from the resolved window unless it is
    configured explicitly, so ``budget // 4`` always follows a model switch.
    """
    context_window = resolve_context_window(environment, model_window=model_window)
    return CompactionConfig(
        enabled=_bool(environment.get("COMPACTION_LAYER_ENABLED"), True, name="ENABLED"),
        l1_enabled=_bool(environment.get("COMPACTION_LAYER_L1_ENABLED"), True, name="L1"),
        l2_enabled=_bool(environment.get("COMPACTION_LAYER_L2_ENABLED"), True, name="L2"),
        l3_enabled=_bool(environment.get("COMPACTION_LAYER_L3_ENABLED"), True, name="L3"),
        l4_enabled=_bool(environment.get("COMPACTION_LAYER_L4_ENABLED"), True, name="L4"),
        reactive_enabled=_bool(
            environment.get("COMPACTION_LAYER_REACTIVE_ENABLED"), True, name="REACTIVE"
        ),
        context_window=context_window,
        keep_recent_tokens=_int(
            environment.get("COMPACTION_LAYER_KEEP_RECENT_TOKENS"),
            context_window // 4,
            name="KEEP_RECENT_TOKENS",
            minimum=0,
        ),
        persist_threshold_chars=_int(
            environment.get("COMPACTION_LAYER_PERSIST_THRESHOLD_CHARS"),
            DEFAULT_PERSIST_THRESHOLD_CHARS,
            name="PERSIST_THRESHOLD_CHARS",
            minimum=1,
        ),
        snip_max_messages=_int(
            environment.get("COMPACTION_LAYER_SNIP_MAX_MESSAGES"),
            DEFAULT_SNIP_MAX_MESSAGES,
            name="SNIP_MAX_MESSAGES",
            minimum=1,
        ),
        placeholder_min_chars=_int(
            environment.get("COMPACTION_LAYER_PLACEHOLDER_MIN_CHARS"),
            DEFAULT_PLACEHOLDER_MIN_CHARS,
            name="PLACEHOLDER_MIN_CHARS",
            minimum=0,
        ),
        keep_recent_results=_int(
            environment.get("COMPACTION_LAYER_KEEP_RECENT_RESULTS"),
            DEFAULT_KEEP_RECENT_RESULTS,
            name="KEEP_RECENT_RESULTS",
            minimum=0,
        ),
        max_output_tokens_for_summary=_int(
            environment.get("COMPACTION_LAYER_MAX_OUTPUT_TOKENS_FOR_SUMMARY"),
            20_000,
            name="MAX_OUTPUT_TOKENS_FOR_SUMMARY",
            minimum=1,
        ),
        max_consecutive_failures=_int(
            environment.get("COMPACTION_LAYER_MAX_CONSECUTIVE_FAILURES"),
            3,
            name="MAX_CONSECUTIVE_FAILURES",
            minimum=1,
        ),
        summary_timeout_seconds=_float(
            environment.get("COMPACTION_LAYER_SUMMARY_TIMEOUT_SECONDS"),
            60.0,
            name="SUMMARY_TIMEOUT_SECONDS",
            minimum=0.001,
        ),
        inline_model_attempt=_bool(
            environment.get("COMPACTION_LAYER_INLINE_MODEL_ATTEMPT"),
            True,
            name="INLINE_MODEL_ATTEMPT",
        ),
    )


__all__ = [
    "BUDGET_THRESHOLD_PERCENT",
    "CONTEXT_WINDOW_ENV",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "CompactionConfig",
    "budget_threshold",
    "load_config",
    "resolve_context_window",
    "should_compact",
]
