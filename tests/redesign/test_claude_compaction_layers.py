"""Pure-layer tests for the four-layer Claude compaction port.

Every layer is driven through its pure functions: the L1 tool-result rules and
token estimator, the L2 boundary/projection/nudge semantics, API-round grouping,
the prompt cleanup, the L3 memory summary and keep-index arithmetic, the L4
window arithmetic with the failure breaker and the reactive classifier, and the
``CustomEntry`` state round-trip. Nothing here touches a model or a session.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from pathlib import Path

import pytest

from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceRequest,
    InferenceResult,
)
from run_agent_coding.paths import RunAgentPaths
from run_agent_core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.session.entries import CustomEntry
from run_agent_extensions.claude_compaction import (
    DEFAULT_SM_COMPACT_CONFIG,
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    TIME_BASED_MC_CLEARED_MESSAGE,
    SMCompactConfig,
    auto_compact_threshold,
    effective_context_window_size,
    estimate_message_tokens,
    format_compact_summary,
    get_compact_user_summary_message,
    group_messages_by_api_round,
    is_context_overflow_error,
    is_media_size_error,
    memory_file_paths,
    message_key,
    microcompact,
    plan_memory_compaction,
    project_snipped_view,
    reactive_reason_for_error_text,
    reactive_reason_for_status,
    read_memory_text,
    select_split_index,
    should_auto_compact,
    snip_compact_if_needed,
)
from run_agent_extensions.claude_compaction.auto import (
    SummaryUnavailable,
    request_summary,
)
from run_agent_extensions.claude_compaction.config import FourLayerConfig
from run_agent_extensions.claude_compaction.grouping import (
    PTL_RETRY_MARKER,
    aligned_start_index,
    truncate_head_for_retry,
)
from run_agent_extensions.claude_compaction.memory_compact import (
    calculate_messages_to_keep_index,
    resolve_last_summarized_index,
)
from run_agent_extensions.claude_compaction.micro import (
    COMPACTABLE_TOOL_NAMES,
    get_tool_results_to_delete,
    rough_token_count_estimation,
)
from run_agent_extensions.claude_compaction.prompt import get_compact_prompt
from run_agent_extensions.claude_compaction.snip import (
    SNIP_NUDGE_TEXT,
    SNIP_NUDGE_THRESHOLD,
    estimate_message_chars,
    expand_to_api_rounds,
    select_all_keys,
    select_keys_for_request,
    should_nudge,
    tokens_freed_for,
)
from run_agent_extensions.claude_compaction.state import (
    SNIP_BOUNDARY_TEXT,
    PreparedSummary,
    SessionState,
    SnipBoundary,
    active_entry_ids,
    legalize_view,
    new_boundary,
    persist_boundary,
    persist_summary,
    protected_prefix_length,
    read_boundary,
    read_summary,
    resolve_first_kept_entry_id,
)

NOW_MS = 1_700_000_000_000
STALE_MS = NOW_MS - 61 * 60_000


def user(text: str, *, timestamp: int = NOW_MS) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def assistant(
    text: str,
    *calls: ToolCall,
    response_id: str | None = None,
    timestamp: int = NOW_MS,
) -> AssistantMessage:
    blocks: list[object] = [TextContent(text=text)] if text else []
    blocks.extend(calls)
    return AssistantMessage(
        content=blocks,
        model="test",
        provider="test",
        stop_reason="toolUse" if calls else "stop",
        response_id=response_id,
        timestamp=timestamp,
    )


def call(call_id: str, name: str = "read") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"path": "file.txt"})


def result(
    call_id: str, text: str, name: str = "read", *, timestamp: int = NOW_MS
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(text=text)],
        timestamp=timestamp,
    )


def tool_view(
    rounds: int, *, size: int = 400, name: str = "read", timestamp: int = NOW_MS
) -> list[object]:
    """``rounds`` API rounds, each an assistant tool call plus its result."""
    messages: list[object] = []
    for index in range(rounds):
        call_id = f"call-{index}"
        messages.append(
            assistant(
                "",
                call(call_id, name),
                response_id=f"resp-{index}",
                timestamp=timestamp,
            )
        )
        messages.append(result(call_id, "x" * size, name, timestamp=timestamp))
    return messages


def is_pairing_legal(messages: list[object] | tuple[object, ...]) -> bool:
    """Every tool call has an adjacent result and no result is orphaned."""
    index = 0
    items = list(messages)
    while index < len(items):
        message = items[index]
        if isinstance(message, AssistantMessage):
            calls = [block.id for block in message.content if isinstance(block, ToolCall)]
            for offset, call_id in enumerate(calls, start=1):
                position = index + offset
                if position >= len(items):
                    return False
                candidate = items[position]
                if (
                    not isinstance(candidate, ToolResultMessage)
                    or candidate.tool_call_id != call_id
                ):
                    return False
            index += len(calls) + 1
            continue
        if isinstance(message, ToolResultMessage):
            return False
        index += 1
    return True


# --------------------------------------------------------------------- L1


def test_rough_estimation_matches_the_reference_rounding() -> None:
    assert rough_token_count_estimation("a" * 400) == 100
    # JS Math.round rounds half up; Python's round() would answer 4 here.
    assert rough_token_count_estimation("a" * 18) == 5
    assert rough_token_count_estimation("") == 0


def test_message_token_estimation_uses_text_thinking_calls_images_and_the_4_3_pad() -> None:
    assert estimate_message_tokens([user("a" * 400)]) == math.ceil(100 * 4 / 3)
    assert estimate_message_tokens([]) == 0
    assert estimate_message_tokens([user("aaaa")]) == math.ceil(1 * 4 / 3)
    image = UserMessage(content=[ImageContent(data="abc", mime_type="image/png")])
    assert estimate_message_tokens([image]) == math.ceil(2_000 * 4 / 3)
    thinking = AssistantMessage(content=[ThinkingContent(thinking="a" * 400)], model="test")
    assert estimate_message_tokens([thinking]) == math.ceil(100 * 4 / 3)
    tool_call = AssistantMessage(
        content=[ToolCall(id="z" * 200, name="read", arguments={"path": "p"})], model="test"
    )
    expected = rough_token_count_estimation("read" + '{"path": "p"}')
    assert estimate_message_tokens([tool_call]) == math.ceil(expected * 4 / 3)


def test_l1_clears_only_compactable_results_and_keeps_the_recent_five() -> None:
    view = [user("head"), *tool_view(12)]
    outcome = microcompact(
        view,
        keep_recent=5,
        cached_trigger_threshold=10,
        now_ms=NOW_MS,
        time_based_enabled=False,
    )

    assert outcome.trigger == "count"
    assert outcome.cleared_ids == tuple(f"call-{index}" for index in range(7))
    assert outcome.tokens_saved > 0
    rewritten = list(outcome.messages)
    placeholders = [
        message
        for message in rewritten
        if isinstance(message, ToolResultMessage) and message.text == TIME_BASED_MC_CLEARED_MESSAGE
    ]
    kept = [
        message
        for message in rewritten
        if isinstance(message, ToolResultMessage) and message.text != TIME_BASED_MC_CLEARED_MESSAGE
    ]
    assert len(placeholders) == 7
    assert len(kept) == 5
    assert rewritten[0] is view[0], "the cacheable prefix is never rewritten"
    assert is_pairing_legal(legalize_view(rewritten))


def test_l1_below_the_count_threshold_clears_nothing() -> None:
    view = [user("head"), *tool_view(10)]
    outcome = microcompact(
        view,
        keep_recent=5,
        cached_trigger_threshold=10,
        now_ms=NOW_MS,
        time_based_enabled=False,
    )

    assert not outcome.applied
    assert outcome.messages == tuple(view)
    assert (
        get_tool_results_to_delete(
            [f"c{index}" for index in range(10)], trigger_threshold=10, keep_recent=5
        )
        == []
    )


def test_l1_time_based_trigger_clears_without_the_count_threshold() -> None:
    stale_view = [user("head"), *tool_view(6, timestamp=STALE_MS)]
    outcome = microcompact(
        stale_view,
        keep_recent=5,
        cached_trigger_threshold=10,
        now_ms=NOW_MS,
        time_based_enabled=True,
        gap_threshold_minutes=60,
    )

    assert outcome.trigger == "time-based"
    assert outcome.cleared_ids == ("call-0",), "everything but the five most recent results"
    assert outcome.gap_minutes is not None and outcome.gap_minutes >= 60

    fresh_view = [user("head"), *tool_view(6)]
    assert not microcompact(
        fresh_view,
        keep_recent=5,
        cached_trigger_threshold=10,
        now_ms=NOW_MS,
        time_based_enabled=True,
        gap_threshold_minutes=60,
    ).applied


def test_l1_leaves_non_compactable_tools_alone() -> None:
    view = [user("head"), *tool_view(12, size=4_000, name="memory")]
    outcome = microcompact(
        view,
        keep_recent=1,
        cached_trigger_threshold=1,
        now_ms=NOW_MS,
        time_based_enabled=False,
    )

    assert "memory" not in COMPACTABLE_TOOL_NAMES
    assert not outcome.applied


def test_protected_prefix_follows_the_leading_role() -> None:
    assert protected_prefix_length([]) == 0
    assert protected_prefix_length([user("first")]) == 1
    assert protected_prefix_length([CompactionSummaryMessage(summary="s", tokens_before=1)]) == 1
    assert protected_prefix_length([assistant("only")]) == 0


# --------------------------------------------------------------------- L2


def test_boundary_payload_has_the_reference_shape() -> None:
    boundary = new_boundary(("cc:a", "cc:b"), trigger="force-snip", tokens_freed=7)
    payload = boundary.payload()

    assert payload["type"] == "system"
    assert payload["subtype"] == "snip_boundary"
    assert payload["content"] == SNIP_BOUNDARY_TEXT
    assert payload["isMeta"] is True
    metadata = payload["snipMetadata"]
    assert isinstance(metadata, dict)
    assert list(metadata["removedUuids"]) == ["cc:a", "cc:b"]
    assert metadata["tokensFreed"] == 7
    restored = SnipBoundary.from_payload(payload)
    assert restored is not None
    assert restored.removed == ("cc:a", "cc:b")
    assert restored.tokens_freed == 7
    assert restored.trigger == "force-snip"


def test_snip_projection_is_idempotent_and_returns_the_input_without_removals() -> None:
    messages = [user("head"), *tool_view(2)]
    assert project_snipped_view(messages, frozenset()) is messages

    removed = frozenset(message_key(message) for message in messages[1:])
    projected = project_snipped_view(messages, removed)
    assert len(projected) == 2, "the protected prefix and the boundary text remain"
    assert projected[0] is messages[0]
    assert projected[1].text == SNIP_BOUNDARY_TEXT
    assert project_snipped_view(projected, removed) == projected


def test_snip_removals_grow_to_whole_api_rounds() -> None:
    messages = [user("head"), *tool_view(3)]
    only_result = message_key(messages[2])
    expanded = expand_to_api_rounds(messages, frozenset({only_result}))

    assert message_key(messages[1]) in expanded, "removing a result removes its call too"
    assert only_result in expanded
    assert is_pairing_legal(project_snipped_view(messages, frozenset({only_result})))


def test_snip_removals_keep_pairs_together_without_swallowing_a_new_prompt() -> None:
    messages = [user("head"), *tool_view(2), user("the next question")]
    removed = frozenset(
        message_key(message) for message in messages[1:5]
    )  # every round before the new prompt

    projected = project_snipped_view(messages, removed)

    texts = [message.text for message in projected]
    assert texts[0] == "head"
    assert SNIP_BOUNDARY_TEXT in texts
    assert texts[-1] == "the next question", "a fresh prompt is never snipped"
    assert is_pairing_legal(projected)

    # Removing only a result still takes its call with it.
    partial = project_snipped_view(messages, frozenset({message_key(messages[2])}))
    assert is_pairing_legal(partial)
    assert message_key(messages[1]) not in {message_key(item) for item in partial}
    assert message_key(messages[2]) not in {message_key(item) for item in partial}
    assert message_key(messages[3]) in {message_key(item) for item in partial}


def test_tokens_freed_follows_the_reference_formula() -> None:
    messages = [user("a" * 400), result("c1", "b" * 8)]
    keys = frozenset(select_all_keys(messages))
    expected = max(1, math.ceil(estimate_message_chars(messages[0]) / 4)) + max(
        1, math.ceil(estimate_message_chars(messages[1]) / 4)
    )

    assert tokens_freed_for(messages, keys) == expected
    assert tokens_freed_for(messages, frozenset()) == 0


def test_snip_compact_if_needed_uses_only_the_last_boundary() -> None:
    messages = [user("head"), *tool_view(2)]
    first = new_boundary((message_key(messages[1]),), trigger="force-snip")
    second = new_boundary((message_key(messages[3]),), trigger="snip")

    result = snip_compact_if_needed(messages, [first, second])

    assert result.executed
    assert result.tokens_freed > 0
    keys = {message_key(item) for item in result.messages}
    assert message_key(messages[3]) not in keys, "the last boundary wins"
    assert message_key(messages[1]) in keys, "an earlier boundary is ignored"

    merged = project_snipped_view(messages, frozenset(first.removed) | frozenset(second.removed))
    merged_keys = {message_key(item) for item in merged}
    assert message_key(messages[1]) not in merged_keys
    assert message_key(messages[3]) not in merged_keys

    untouched = snip_compact_if_needed(messages, [])
    assert not untouched.executed
    assert untouched.messages == messages


def test_snip_nudge_threshold_is_thirty_messages() -> None:
    assert SNIP_NUDGE_THRESHOLD == 30
    assert not should_nudge([user("x") for _ in range(29)])
    assert should_nudge([user("x") for _ in range(30)])
    assert SNIP_NUDGE_TEXT.startswith("The conversation history is getting long.")


def test_snip_tool_selection_accepts_ids_ordinals_ranges_and_keep_recent() -> None:
    messages = [user("head"), *tool_view(3)]

    assert select_keys_for_request(messages, message_ids=["m2"]) == (message_key(messages[1]),)
    assert select_keys_for_request(messages, message_ids=[message_key(messages[2])]) == (
        message_key(messages[2]),
    )
    assert select_keys_for_request(messages, message_ids=["nope"]) == ()
    assert select_keys_for_request(messages, range_start=1, range_end=2) == (
        message_key(messages[0]),
        message_key(messages[1]),
    )
    assert select_keys_for_request(messages, keep_recent=2) == tuple(
        message_key(message) for message in messages[:-2]
    )


# ---------------------------------------------------------------- grouping


def test_grouping_starts_a_new_group_per_assistant_response() -> None:
    messages = [user("q1"), *tool_view(2), user("q2"), assistant("answer")]
    groups = group_messages_by_api_round(messages)

    # A group ends only when a *new* assistant response starts, so a trailing user
    # message joins the round it follows (exactly as in grouping.ts).
    assert [[message.role for message in group] for group in groups] == [
        ["user"],
        ["assistant", "toolResult"],
        ["assistant", "toolResult", "user"],
        ["assistant"],
    ]


def test_grouping_keeps_streamed_chunks_of_one_response_together() -> None:
    messages: list[object] = [
        user("q"),
        assistant("thinking", response_id="resp-shared"),
        assistant("", call("call-a"), response_id="resp-shared"),
        result("call-a", "a"),
        assistant("", call("call-b"), response_id="resp-shared"),
        result("call-b", "b"),
    ]
    groups = group_messages_by_api_round(messages)

    assert [len(group) for group in groups] == [1, 5]
    assert aligned_start_index(messages, 3) == 1
    assert aligned_start_index(messages, 1) == 1
    assert aligned_start_index(messages, 6) == 6


def test_groups_without_a_response_id_do_not_collide() -> None:
    messages = [user("q"), assistant("first"), user("again"), assistant("second")]
    # Two distinct assistant messages are two rounds even without provider ids;
    # the user message after the first reply joins that reply's round.
    groups = group_messages_by_api_round(messages)
    assert [len(group) for group in groups] == [1, 2, 1]


def test_truncate_head_for_retry_drops_whole_groups_and_re_asserts_a_user_head() -> None:
    messages = [user("q1"), *tool_view(4)]
    truncated = truncate_head_for_retry(messages, drop_fraction=0.5)

    assert truncated, "a truncated retry must leave something to summarize"
    assert len(truncated) < len(messages)
    assert truncated[0].text in {messages[0].text, PTL_RETRY_MARKER}
    assert truncate_head_for_retry([user("only")]) == []

    by_gap = truncate_head_for_retry([user("q1"), *tool_view(4, size=4_000)], token_gap=1)
    assert len(by_gap) > len(truncated), "the token gap drops the smallest possible prefix"


def test_select_split_index_keeps_a_round_aligned_tail() -> None:
    messages = [user("q1"), *tool_view(4, size=4_000)]
    index = select_split_index(messages, keep_recent_tokens=1_000)

    assert 0 < index < len(messages)
    assert index == aligned_start_index(messages, index)
    assert is_pairing_legal(legalize_view(messages[index:]))
    assert select_split_index([user("only")], keep_recent_tokens=1_000) == 1


# ---------------------------------------------------------------- prompt


def test_format_compact_summary_strips_analysis_and_rewrites_summary_tags() -> None:
    raw = (
        "<analysis>\ndrafting notes\n</analysis>\n\n"
        "<summary>\n1. Goal: ship it\n\n\n2. Work: done\n</summary>\n"
    )
    formatted = format_compact_summary(raw)

    assert "drafting notes" not in formatted
    assert "<summary>" not in formatted
    assert formatted.startswith("Summary:\n1. Goal: ship it")
    assert "\n\n\n" not in formatted
    assert format_compact_summary("  plain summary  ") == "plain summary"


def test_continuation_message_carries_the_reference_text() -> None:
    message = get_compact_user_summary_message(
        "<summary>all done</summary>",
        suppress_follow_up_questions=True,
        transcript_path="C:/sessions/one.jsonl",
        recent_messages_preserved=True,
    )

    assert "The summary below covers the earlier portion of the conversation." in message
    assert "read the full transcript at: C:/sessions/one.jsonl" in message
    assert "Recent messages are preserved verbatim." in message
    assert "as if the break never happened." in message
    assert "Summary:\nall done" in message
    quiet = get_compact_user_summary_message("text", suppress_follow_up_questions=False)
    assert "without asking the user any further questions" not in quiet


def test_compact_prompt_carries_the_no_tools_preamble_trailer_and_instructions() -> None:
    prompt = get_compact_prompt("Focus on TypeScript changes.")

    assert prompt.startswith("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.")
    assert prompt.rstrip().endswith("Tool calls will be rejected and you will fail the task.")
    assert "Additional Instructions:\nFocus on TypeScript changes." in prompt
    assert get_compact_prompt().endswith("you will fail the task.")


# --------------------------------------------------------------------- L3


def test_memory_file_paths_follow_run_agent_paths(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")

    resolved = memory_file_paths(paths, tmp_path / "project")

    assert resolved == (
        paths.home / "MEMORY.md",
        paths.home / "USER.md",
        tmp_path / "project" / ".run" / "MEMORY.md",
        tmp_path / "project" / ".run" / "USER.md",
    )


def test_memory_text_reads_both_scopes_and_skips_empty_files(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")
    paths.home.mkdir(parents=True)
    (paths.home / "MEMORY.md").write_text("user memory", encoding="utf-8")
    (paths.home / "USER.md").write_text("   \n", encoding="utf-8")
    project_run = paths.project_run_agent_dir(tmp_path)
    project_run.mkdir(parents=True)
    (project_run / "USER.md").write_text("project user", encoding="utf-8")

    memory = read_memory_text(paths, tmp_path)

    assert "user memory" in memory.text
    assert "project user" in memory.text
    assert str(paths.home / "MEMORY.md") in memory.text
    assert memory.sources == (paths.home / "MEMORY.md", project_run / "USER.md")
    assert not memory.empty
    elsewhere = read_memory_text(paths, tmp_path / "elsewhere")
    assert elsewhere.sources == (paths.home / "MEMORY.md",)


def test_last_summarized_index_models_the_resumed_case_and_missing_keys() -> None:
    messages = [user("q1"), *tool_view(1), user("q2")]

    assert resolve_last_summarized_index(messages, None) == len(messages) - 1
    assert resolve_last_summarized_index(messages, message_key(messages[1])) == 1
    assert resolve_last_summarized_index(messages, "cc:missing") == -1


def test_l3_summary_reuses_memory_content_and_keeps_a_round_aligned_tail() -> None:
    empty = read_memory_text(RunAgentPaths(home=Path("nonexistent")), Path("nonexistent"))
    assert empty.empty
    memory = replace(
        empty, text="## MEMORY.md\n\nRuns pytest with -q", sources=(Path("MEMORY.md"),)
    )
    messages = [
        user("x" * 4_000),
        assistant("first answer"),
        user("y" * 4_000),
        assistant("second"),
    ]
    config = SMCompactConfig(min_tokens=1_000, min_text_block_messages=1, max_tokens=1_000_000)

    plan = plan_memory_compaction(
        messages,
        memory=memory,
        last_summarized_key=None,
        transcript_path="C:/sessions/one.jsonl",
        config=config,
    )

    assert plan is not None
    assert plan.keep_index == 1, (
        "the boundary lands on an API-round start; the user message after the first "
        "reply belongs to that reply's round, so the tail starts one message earlier"
    )
    assert plan.replaced_rows == plan.keep_index
    assert "Runs pytest with -q" in plan.summary
    assert "The summary below covers the earlier portion" in plan.summary
    assert "C:/sessions/one.jsonl" in plan.summary
    assert plan.sources == (Path("MEMORY.md"),)


def test_l3_falls_back_to_l4_on_empty_memory_missing_key_or_an_unsatisfiable_threshold(
    tmp_path: Path,
) -> None:
    messages = [user("x" * 4_000), assistant("first"), user("y" * 4_000), assistant("second")]
    config = SMCompactConfig(min_tokens=1_000, min_text_block_messages=1, max_tokens=1_000_000)

    empty = read_memory_text(RunAgentPaths(home=tmp_path / "none"), tmp_path)
    assert (
        plan_memory_compaction(messages, memory=empty, last_summarized_key=None, config=config)
        is None
    )

    memory = replace(empty, text="memory", sources=(tmp_path / "MEMORY.md",))
    assert (
        plan_memory_compaction(
            messages, memory=memory, last_summarized_key="cc:gone", config=config
        )
        is None
    )
    assert (
        plan_memory_compaction(
            messages, memory=memory, last_summarized_key=None, config=config, threshold=1
        )
        is None
    )
    assert (
        plan_memory_compaction(
            messages, memory=memory, last_summarized_key=None, config=DEFAULT_SM_COMPACT_CONFIG
        )
        is None
    ), "the reference's 10k minimum keeps a small session unchanged"


def test_l3_keep_index_expands_backwards_to_the_minimums() -> None:
    messages = [user("q1"), assistant("a1"), user("q2"), assistant("a2")]
    config = SMCompactConfig(min_tokens=0, min_text_block_messages=0, max_tokens=10_000)

    assert calculate_messages_to_keep_index(messages, -1, config) == len(messages)
    assert calculate_messages_to_keep_index(messages, 1, config) == 2
    expanding = SMCompactConfig(min_tokens=1, min_text_block_messages=5, max_tokens=10_000)
    assert calculate_messages_to_keep_index(messages, 3, expanding) == 0


# --------------------------------------------------------------------- L4


def test_effective_window_and_threshold_arithmetic() -> None:
    assert MAX_OUTPUT_TOKENS_FOR_SUMMARY == 20_000
    assert effective_context_window_size(200_000, 200_000) == 180_000
    assert effective_context_window_size(200_000, 8_000) == 192_000

    default = FourLayerConfig(context_window=200_000, max_output_tokens_for_summary=20_000)
    assert auto_compact_threshold(default) == 180_000 - 13_000
    big = FourLayerConfig(context_window=900_000, max_output_tokens_for_summary=20_000)
    assert auto_compact_threshold(big) == 880_000 - 50_000
    mid = FourLayerConfig(context_window=500_000, max_output_tokens_for_summary=20_000)
    assert auto_compact_threshold(mid) == 480_000 - 30_000
    override = replace(default, autocompact_pct_override=50.0)
    assert auto_compact_threshold(override) == min(90_000, 167_000)
    high = replace(default, autocompact_pct_override=99.0)
    assert auto_compact_threshold(high) == min(math.floor(180_000 * 0.99), 167_000)


def test_should_auto_compact_respects_the_layer_switches() -> None:
    config = FourLayerConfig(context_window=200_000, max_output_tokens_for_summary=20_000)
    threshold = auto_compact_threshold(config)

    assert not should_auto_compact(threshold - 1, config)
    assert should_auto_compact(threshold, config)
    assert not should_auto_compact(threshold + 1, replace(config, l4_enabled=False))
    assert not should_auto_compact(threshold + 1, replace(config, enabled=False))


def test_reactive_classification_is_conservative_on_http_status() -> None:
    assert reactive_reason_for_status(413) == "media_size"
    assert reactive_reason_for_status(400) == "prompt_too_long_suspected"
    assert reactive_reason_for_status(200) is None
    assert is_context_overflow_error("prompt is too long: 200001 tokens")
    assert not is_context_overflow_error("rate limited")
    assert is_media_size_error("image exceeds the maximum size")
    assert not is_media_size_error("image not found")
    assert reactive_reason_for_error_text("Prompt is too long") == "prompt_too_long"
    assert reactive_reason_for_error_text("media too large") == "media_size"
    assert reactive_reason_for_error_text("bad request") is None


def test_the_failure_breaker_trips_after_three_consecutive_failures() -> None:
    state = SessionState(config=FourLayerConfig(max_consecutive_failures=3))

    for expected in (1, 2):
        assert state.note_failure("timeout") == expected
        assert not state.breaker_tripped()
    assert state.note_failure("timeout") == MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    assert state.breaker_tripped()
    assert state.breaker_reason == "timeout"

    state.note_success()
    assert not state.breaker_tripped()
    assert state.consecutive_failures == 0


class StubInference:
    """An offline ``InferenceService``: scripted results, no provider."""

    def __init__(self, *, text: str = "summary", errors: list[Exception] | None = None) -> None:
        self.text = text
        self.errors = list(errors or [])
        self.requests: list[InferenceRequest] = []
        self.delay = 0.0

    @property
    def available(self) -> bool:
        return True

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.errors:
            raise self.errors.pop(0)
        return InferenceResult(
            text=self.text,
            model="test",
            snapshot_id="snap-1",
            input_tokens=10,
            output_tokens=5,
        )


async def test_request_summary_reports_its_bookkeeping() -> None:
    inference = StubInference(text="## Goal\nship it")
    config = FourLayerConfig(summary_timeout_seconds=1.0)

    result = await request_summary(inference, [user("hello")], config=config)

    assert result.text == "## Goal\nship it"
    assert (result.model, result.snapshot_id) == ("test", "snap-1")
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    assert result.attempts == 1
    assert inference.requests[0].purpose == "claude_compaction_summary"
    assert inference.requests[0].max_output_tokens == config.max_output_tokens_for_summary
    assert "CRITICAL: Respond with TEXT ONLY" in inference.requests[0].prompt


async def test_request_summary_times_out_on_empty_text_and_lets_busy_through() -> None:
    slow = StubInference()
    slow.delay = 0.2
    with pytest.raises(SummaryUnavailable, match="timed out"):
        await request_summary(
            slow, [user("hello")], config=FourLayerConfig(summary_timeout_seconds=0.01)
        )

    with pytest.raises(SummaryUnavailable, match="no text"):
        await request_summary(StubInference(text="   "), [user("hello")], config=FourLayerConfig())

    with pytest.raises(InferenceBusy):
        await request_summary(
            StubInference(errors=[InferenceBusy("foreground run")]),
            [user("hello")],
            config=FourLayerConfig(),
        )


async def test_request_summary_retries_prompt_too_long_with_a_truncated_head() -> None:
    messages = [user("q1"), *tool_view(4, size=4_000), user("q2")]
    inference = StubInference(errors=[RuntimeError("prompt is too long: 300000 tokens")])

    result = await request_summary(inference, messages, config=FourLayerConfig())

    assert result.attempts == 2
    assert len(inference.requests) == 2
    assert PTL_RETRY_MARKER in inference.requests[1].prompt


async def test_request_summary_gives_up_after_the_retry_budget() -> None:
    messages = [user("q1"), *tool_view(4, size=4_000), user("q2")]
    errors: list[Exception] = [RuntimeError("prompt is too long") for _ in range(4)]

    with pytest.raises(SummaryUnavailable, match="prompt is too long"):
        await request_summary(StubInference(errors=errors), messages, config=FourLayerConfig())


# ------------------------------------------------------------------ state


def test_message_key_is_stable_and_content_sensitive() -> None:
    first = user("same text")

    assert message_key(first) == message_key(first.model_copy(deep=True))
    assert message_key(first) != message_key(user("other text"))
    assert message_key(first).startswith("cc:")


async def test_custom_entry_round_trip_for_boundaries_and_summaries() -> None:
    stored: dict[str, CustomEntry] = {}

    async def append(namespace: str, data: dict[str, object]) -> str:
        entry = CustomEntry(namespace=namespace, data=data)  # type: ignore[arg-type]
        stored[entry.id] = entry
        return entry.id

    async def read(entry_id: str) -> CustomEntry:
        return stored[entry_id]

    boundary = new_boundary(("cc:one",), trigger="tool", reason="trim", tokens_freed=12)
    boundary_id = await persist_boundary(append, boundary)  # type: ignore[arg-type]
    summary = PreparedSummary(
        text="Summary:\nall done",
        trigger="auto",
        tokens_before=123,
        replaced_rows=4,
        created_at=1.0,
        anchor_key="cc:anchor",
        model="test",
        snapshot_id="snap-1",
        layer="L4",
    )
    summary_id = await persist_summary(append, summary)  # type: ignore[arg-type]

    assert stored[boundary_id].namespace == "claude_compaction.snip"
    assert stored[summary_id].namespace == "claude_compaction.summary"
    assert await read_boundary(read, boundary_id) == boundary  # type: ignore[arg-type]
    assert await read_summary(read, summary_id) == summary  # type: ignore[arg-type]
    assert SnipBoundary.from_payload({"subtype": "snip_boundary"}) is None
    assert PreparedSummary.from_payload({"subtype": "prepared_summary", "text": " "}) is None


def test_active_entry_ids_and_boundary_resolution_are_conservative() -> None:
    assert active_entry_ids({}) == ()
    assert active_entry_ids({"context_entry_ids": ["a", 1, "b"]}) == ("a", "b")
    assert active_entry_ids({"context_entry_ids": "nope"}) == ()

    ids = ("e0", "e1", "e2")
    assert resolve_first_kept_entry_id(ids, 0) is None
    assert resolve_first_kept_entry_id(ids, 1) == "e1"
    assert resolve_first_kept_entry_id(ids, 99) == "e2"
    assert resolve_first_kept_entry_id(("e0",), 1) is None
