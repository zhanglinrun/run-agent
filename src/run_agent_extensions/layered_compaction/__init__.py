"""Cheap-first layered compaction on the Run Agent extension seam.

A self-contained package that owns request-view compaction: a token gate, the
three free layers (L1 persist oversized tool results, L2 snip the middle of a
long view, L3 placeholder old tool results) and the paid L4 model summary, plus
the reactive path; wired into ``before_provider_request`` by :func:`setup`. The
package is a built-in extension (``run_agent_extensions.BUILTIN_EXTENSIONS``,
short name ``compaction``) that loads by default; loading it is what gives a
session compaction, and it can also be loaded explicitly through
``--extension compaction`` or by pointing at this directory.

The layer numbers are the *execution* order. The reference implementation this
strategy comes from numbers the same three free strategies ``L3``/``L1``/``L2``
and the summary ``L4``; the README explains that mapping.

Memory is not a compaction layer: the memory extension's pre-compression text
reaches the summarizer prompt as fenced material through the
`session_before_compact` gate (see :func:`render_memory_provider_context`).

Only stable names are re-exported here.
"""

from __future__ import annotations

from .config import (
    BUDGET_THRESHOLD_PERCENT,
    CONTEXT_WINDOW_ENV,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    CompactionConfig,
    budget_threshold,
    load_config,
    resolve_context_window,
    should_compact,
)
from .extension import (
    COMPACT_COMMAND,
    COMPACT_RESULT_TEMPLATE,
    COMPACT_USAGE,
    DISABLED_NOTE,
    PERSISTED_OUTPUT_GUIDELINE,
    TOOL_RESULTS_DIRECTORY,
    LayeredCompaction,
    setup,
)
from .grouping import (
    MAX_PTL_RETRIES,
    PTL_RETRY_MARKER,
    group_messages_by_api_round,
    truncate_head_for_retry,
)
from .layers import (
    DEFAULT_KEEP_RECENT_RESULTS,
    DEFAULT_PERSIST_THRESHOLD_CHARS,
    DEFAULT_PLACEHOLDER_MIN_CHARS,
    DEFAULT_SNIP_MAX_MESSAGES,
    EARLIER_TOOL_RESULT_PLACEHOLDER,
    PERSISTED_OUTPUT_MARKER,
    PERSISTED_OUTPUT_TEMPLATE,
    SNIPPED_MIDDLE_TEMPLATE,
    PersistOutcome,
    PlaceholderOutcome,
    SnipOutcome,
    estimate_message_tokens,
    estimate_request_tokens,
    persist_oversized_results,
    placeholder_old_results,
    rough_token_count_estimation,
    run_free_layers,
    snap_to_pair_boundary,
    snip_middle,
)
from .prompt import (
    SUMMARIZATION_PROMPT_TEMPLATE,
    SUMMARIZATION_SYSTEM_PROMPT,
    SUMMARY_SECTIONS,
    build_summary_prompt,
    extract_summary,
)
from .state import (
    STATUS_NAMESPACE,
    SUMMARY_NAMESPACE,
    PreparedSummary,
    SessionState,
    active_entry_ids,
    legalize_view,
    message_key,
    persist_summary,
    resolve_first_kept_entry_id,
    summary_message_text,
    view_summary_text,
)
from .summary import (
    MEMORY_PROVIDER_CONTEXT_CLOSE,
    MEMORY_PROVIDER_CONTEXT_OPEN,
    SUMMARY_PURPOSE,
    SummaryResult,
    SummaryUnavailable,
    build_summary_request,
    is_context_overflow_error,
    is_media_size_error,
    reactive_reason_for_error_text,
    reactive_reason_for_status,
    render_memory_provider_context,
    request_summary,
    select_cut_index,
    serialize_conversation,
)

__all__ = [
    "BUDGET_THRESHOLD_PERCENT",
    "COMPACT_COMMAND",
    "COMPACT_RESULT_TEMPLATE",
    "COMPACT_USAGE",
    "CONTEXT_WINDOW_ENV",
    "CompactionConfig",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_KEEP_RECENT_RESULTS",
    "DEFAULT_PERSIST_THRESHOLD_CHARS",
    "DEFAULT_PLACEHOLDER_MIN_CHARS",
    "DEFAULT_SNIP_MAX_MESSAGES",
    "DISABLED_NOTE",
    "EARLIER_TOOL_RESULT_PLACEHOLDER",
    "LayeredCompaction",
    "MAX_PTL_RETRIES",
    "MEMORY_PROVIDER_CONTEXT_CLOSE",
    "MEMORY_PROVIDER_CONTEXT_OPEN",
    "PERSISTED_OUTPUT_GUIDELINE",
    "PERSISTED_OUTPUT_MARKER",
    "PERSISTED_OUTPUT_TEMPLATE",
    "PTL_RETRY_MARKER",
    "PersistOutcome",
    "PlaceholderOutcome",
    "PreparedSummary",
    "SNIPPED_MIDDLE_TEMPLATE",
    "STATUS_NAMESPACE",
    "SUMMARY_NAMESPACE",
    "SUMMARY_PURPOSE",
    "SUMMARY_SECTIONS",
    "SUMMARIZATION_PROMPT_TEMPLATE",
    "SUMMARIZATION_SYSTEM_PROMPT",
    "SessionState",
    "SnipOutcome",
    "SummaryResult",
    "SummaryUnavailable",
    "TOOL_RESULTS_DIRECTORY",
    "active_entry_ids",
    "budget_threshold",
    "build_summary_prompt",
    "build_summary_request",
    "estimate_message_tokens",
    "estimate_request_tokens",
    "extract_summary",
    "group_messages_by_api_round",
    "is_context_overflow_error",
    "is_media_size_error",
    "legalize_view",
    "load_config",
    "message_key",
    "persist_oversized_results",
    "persist_summary",
    "placeholder_old_results",
    "reactive_reason_for_error_text",
    "reactive_reason_for_status",
    "render_memory_provider_context",
    "request_summary",
    "resolve_context_window",
    "resolve_first_kept_entry_id",
    "rough_token_count_estimation",
    "run_free_layers",
    "select_cut_index",
    "serialize_conversation",
    "setup",
    "should_compact",
    "snap_to_pair_boundary",
    "snip_middle",
    "summary_message_text",
    "truncate_head_for_retry",
    "view_summary_text",
]