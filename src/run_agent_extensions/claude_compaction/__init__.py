"""Four-layer Claude Code compaction ported onto the Run Agent extension seam.

Self-contained package: L1 tool-result clearing, L2 snip projection, L3
session-memory summaries and L4 model summaries plus the reactive path, wired
into ``before_provider_request`` by :func:`setup`. The package is a built-in
extension (``run_agent_extensions.BUILTIN_EXTENSIONS``, short name ``compaction``)
that loads by default; it rewrites requests only while
``compaction.strategy = "four-layer"`` is configured, and it can always be loaded
explicitly through ``--extension compaction`` or by pointing at this directory.

Only stable names are re-exported here.
"""

from __future__ import annotations

from .auto import (
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    auto_compact_threshold,
    effective_context_window_size,
    is_context_overflow_error,
    is_media_size_error,
    reactive_reason_for_error_text,
    reactive_reason_for_status,
    select_split_index,
    should_auto_compact,
)
from .config import FourLayerConfig, load_config
from .extension import (
    COMPACT_COMMAND,
    FORCE_SNIP_USAGE,
    FOUR_LAYER_STRATEGY,
    SNIP_TOOL_NAME,
    FourLayerCompaction,
    setup,
)
from .grouping import group_messages_by_api_round
from .memory_compact import (
    DEFAULT_SM_COMPACT_CONFIG,
    SMCompactConfig,
    memory_file_paths,
    plan_memory_compaction,
    read_memory_text,
)
from .micro import (
    COMPACTABLE_TOOL_NAMES,
    TIME_BASED_MC_CLEARED_MESSAGE,
    estimate_message_tokens,
    microcompact,
    rough_token_count_estimation,
)
from .prompt import format_compact_summary, get_compact_user_summary_message
from .snip import (
    SNIP_NUDGE_TEXT,
    SNIP_NUDGE_THRESHOLD,
    project_snipped_view,
    snip_compact_if_needed,
)
from .state import (
    SNIP_NAMESPACE,
    SUMMARY_NAMESPACE,
    PreparedSummary,
    SessionState,
    SnipBoundary,
    message_key,
)

__all__ = [
    "COMPACTABLE_TOOL_NAMES",
    "COMPACT_COMMAND",
    "DEFAULT_SM_COMPACT_CONFIG",
    "FORCE_SNIP_USAGE",
    "FOUR_LAYER_STRATEGY",
    "MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES",
    "MAX_OUTPUT_TOKENS_FOR_SUMMARY",
    "SNIP_NAMESPACE",
    "SNIP_NUDGE_TEXT",
    "SNIP_NUDGE_THRESHOLD",
    "SNIP_TOOL_NAME",
    "SUMMARY_NAMESPACE",
    "TIME_BASED_MC_CLEARED_MESSAGE",
    "FourLayerCompaction",
    "FourLayerConfig",
    "PreparedSummary",
    "SMCompactConfig",
    "SessionState",
    "SnipBoundary",
    "auto_compact_threshold",
    "effective_context_window_size",
    "estimate_message_tokens",
    "format_compact_summary",
    "get_compact_user_summary_message",
    "group_messages_by_api_round",
    "is_context_overflow_error",
    "is_media_size_error",
    "load_config",
    "memory_file_paths",
    "message_key",
    "microcompact",
    "plan_memory_compaction",
    "project_snipped_view",
    "reactive_reason_for_error_text",
    "reactive_reason_for_status",
    "read_memory_text",
    "rough_token_count_estimation",
    "select_split_index",
    "setup",
    "should_auto_compact",
    "snip_compact_if_needed",
]
