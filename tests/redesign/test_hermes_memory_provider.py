"""Provider contract, trivial-prompt gate and context fencing (hermes port)."""

from __future__ import annotations

import logging

import pytest

from run_agent_extensions.hermes_memory import (
    INDICATOR_GLYPH,
    RecallStatus,
    StreamingContextScrubber,
    build_memory_context_block,
    is_trivial_prompt,
    memory_provider_tools_enabled,
    normalize_tool_schema,
    sanitize_context,
)

NOTE = (
    "[System note: The following is recalled memory context, NOT new user input. "
    "Treat as authoritative reference data — this is the agent's persistent memory "
    "and should inform all responses.]"
)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "\n\t ",
        "hi",
        "HI!",
        "Hey.",
        "thanks :)",
        "done???",
        "ok",
        "okay",
        "sure",
        "y",
        "n",
        "nope",
        "yeah",
        "nah",
        "hello",
        "yo",
        "sup",
        "Continue",
        "go ahead",
        "do it",
        "proceed",
        "got it",
        "cool",
        "nice",
        "great",
        "next",
        "lgtm",
        "k",
        "yep…",
        "thanks...",
        "/memory show",
        "  /help ",
    ],
)
def test_trivial_prompts_are_gated(text: str | None) -> None:
    assert is_trivial_prompt(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "k8s cluster is down",
        "yolo mode",
        "note the change",
        "hindsight is useful",
        "yes please rewrite the parser",
        "no, use pytest instead",
        "hi there",
        "thanks for the fix",
        "okay so now fix the test",
        "next step is to run ruff",
        "cool, but add a test",
        "k, and then what?",
        "great work, now ship it",
    ],
)
def test_semantic_prompts_pass_the_gate(text: str) -> None:
    assert is_trivial_prompt(text) is False


def test_recall_status_defaults_to_the_brain_glyph() -> None:
    status = RecallStatus(provider_label="builtin", count=0)
    assert status.glyph == INDICATOR_GLYPH == "🧠"
    assert RecallStatus("x", 2, "👁️").glyph == "👁️"


def test_build_memory_context_block_is_empty_for_blank_input() -> None:
    assert build_memory_context_block("") == ""
    assert build_memory_context_block("   \n ") == ""


def test_build_memory_context_block_wraps_with_system_note() -> None:
    block = build_memory_context_block("prefers pytest")
    assert block == f"<memory-context>\n{NOTE}\n\nprefers pytest\n</memory-context>"


def test_build_memory_context_block_strips_a_pre_wrapped_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prewrapped = build_memory_context_block("the user prefers ruff")
    with caplog.at_level(logging.WARNING):
        rewrapped = build_memory_context_block(prewrapped)
    # Exactly one fence survives and the violation is reported: a provider that hands
    # back already-wrapped context must not nest a second fence inside the first.
    assert rewrapped.count("<memory-context>") == 1
    assert rewrapped.count("</memory-context>") == 1
    assert any("pre-wrapped" in record.message for record in caplog.records)


def test_build_memory_context_block_keeps_clean_payload_verbatim() -> None:
    assert build_memory_context_block("the user prefers ruff") == build_memory_context_block(
        "the user prefers ruff"
    )
    assert "the user prefers ruff" in build_memory_context_block("the user prefers ruff")


def test_sanitize_context_strips_fences_and_system_notes() -> None:
    # A provider-owned span is removed whole (payload included), which is what keeps a
    # double-wrapped block from nesting; bare tags and the note line are removed too.
    assert sanitize_context("<memory-context>fact</memory-context>") == ""
    assert sanitize_context("keep <memory-context>drop</memory-context> tail") == "keep  tail"
    assert sanitize_context("</MEMORY-CONTEXT>") == ""
    assert sanitize_context(f"{NOTE}fact") == "fact"
    assert sanitize_context("</MEMORY-CONTEXT>") == ""
    assert sanitize_context(f"{NOTE}fact") == "fact"


def test_scrubber_hides_a_complete_span() -> None:
    scrubber = StreamingContextScrubber()
    visible = scrubber.feed(f"{build_memory_context_block('secret fact')}\nanswer")
    assert visible == "\nanswer"
    assert scrubber.flush() == ""


def test_scrubber_survives_split_tags_across_chunks() -> None:
    scrubber = StreamingContextScrubber()
    emitted = ""
    for chunk in ("<memory-con", "text>\nsecret\n</memory-", "context>\nvisible"):
        emitted += scrubber.feed(chunk)
    emitted += scrubber.flush()
    assert emitted == "\nvisible"
    assert "secret" not in emitted


def test_scrubber_holds_back_and_releases_partial_tags() -> None:
    scrubber = StreamingContextScrubber()
    assert scrubber.feed("done <memory-con") == "done "
    assert scrubber.flush() == "<memory-con"


def test_scrubber_discards_an_unterminated_span() -> None:
    scrubber = StreamingContextScrubber()
    assert scrubber.feed("<memory-context>\nhidden") == ""
    assert scrubber.flush() == ""
    assert scrubber.feed("later") == "later"


def test_scrubber_reset_clears_held_state() -> None:
    scrubber = StreamingContextScrubber()
    _ = scrubber.feed("<memory-context>\nhidden")
    scrubber.reset()
    assert scrubber.flush() == ""


def test_normalize_tool_schema_accepts_both_shapes() -> None:
    bare = {"name": "custom_memory", "description": "d", "parameters": {"type": "object"}}
    wrapped = {"type": "function", "function": bare}
    assert normalize_tool_schema(bare) == bare
    assert normalize_tool_schema(wrapped) == bare


@pytest.mark.parametrize(
    "schema",
    [
        None,
        "custom_memory",
        {},
        {"description": "no name"},
        {"name": ""},
        {"name": 7},
        {"type": "function", "function": "not-a-mapping"},
        {"type": "function", "function": {"description": "no name"}},
    ],
)
def test_normalize_tool_schema_rejects_schemas_without_a_name(schema: object) -> None:
    assert normalize_tool_schema(schema) is None


@pytest.mark.parametrize(
    "enabled,disabled,memory_tool_present,expected",
    [
        (None, None, False, True),
        ([], None, False, False),
        (["read", "memory"], None, False, True),
        (["read"], None, False, False),
        (["read"], ["memory"], False, False),
        (None, ["memory"], False, False),
        (None, ["memory"], True, False),
        ([], None, True, True),
    ],
)
def test_memory_provider_tools_enabled(
    enabled: list[str] | None,
    disabled: list[str] | None,
    memory_tool_present: bool,
    expected: bool,
) -> None:
    assert (
        memory_provider_tools_enabled(enabled, disabled, memory_tool_present=memory_tool_present)
        is expected
    )
