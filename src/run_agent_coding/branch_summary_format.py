"""Turning a branch's messages into the prompt and context a summary is built from.

Split out of ``branch_summary`` because that module's job is to call a model, while this
one is pure transformation: messages in, text out, no provider involved. Keeping them
together meant the file grew with every formatting tweak and the model call was buried
under a hundred lines of string building.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    message_text,
)

BRANCH_SUMMARY_PREAMBLE = (
    "The user explored a different conversation branch before returning here.\n"
    "Summary of that exploration:\n\n"
)

BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context
when returning later.

Use this EXACT format:

## Goal
[What was the user trying to accomplish in this branch?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Work that was started but not finished]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next to continue this work]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

MAX_SUMMARY_SOURCE_MESSAGE_CHARS = 4_000
MAX_SUMMARY_SOURCE_TOTAL_CHARS = 60_000
TOOL_RESULT_MAX_CHARS = 2_000


def branch_summary_prompt(
    messages: Sequence[AgentMessage],
    *,
    custom_instructions: str | None = None,
    replace_instructions: bool = False,
) -> str:
    """The conversation, wrapped in the instructions that ask for a structured summary."""
    conversation = _serialize_branch_conversation(messages)
    if replace_instructions and custom_instructions:
        instructions = custom_instructions
    elif custom_instructions:
        instructions = f"{BRANCH_SUMMARY_PROMPT}\n\nAdditional focus: {custom_instructions}"
    else:
        instructions = BRANCH_SUMMARY_PROMPT
    return f"<conversation>\n{conversation}\n</conversation>\n\n{instructions}"


def add_branch_summary_context(summary: str, messages: Sequence[AgentMessage]) -> str:
    """Attach the files the branch touched, so the summary does not have to list them."""
    read_files, modified_files = _branch_file_operations(messages)
    sections = [BRANCH_SUMMARY_PREAMBLE + summary]
    if read_files:
        sections.append(f"<read-files>\n{'\n'.join(read_files)}\n</read-files>")
    if modified_files:
        sections.append(f"<modified-files>\n{'\n'.join(modified_files)}\n</modified-files>")
    return "\n\n".join(sections)


def _serialize_branch_conversation(messages: Sequence[AgentMessage]) -> str:
    parts: list[str] = []
    remaining_chars = MAX_SUMMARY_SOURCE_TOTAL_CHARS
    omitted_count = 0

    for index, message in enumerate(messages, start=1):
        rendered = _format_summary_source_message(message)
        if len(rendered) > remaining_chars:
            omitted_count = len(messages) - index + 1
            break
        parts.append(rendered)
        remaining_chars -= len(rendered)

    if omitted_count:
        parts.append(f"[... {omitted_count} message(s) omitted because the branch was too long]")

    return "\n\n".join(parts)


def _format_summary_source_message(message: AgentMessage) -> str:
    match message:
        case UserMessage():
            return f"[User]: {_trim_summary_source_text(message.text)}"
        case AssistantMessage():
            return _format_assistant_summary_source(message)
        case ToolResultMessage():
            status = "failed" if message.is_error else "ok"
            content = _trim_summary_source_text(message.text, max_chars=TOOL_RESULT_MAX_CHARS)
            return f"[Tool result: {message.tool_name} ({status})]: {content}"
        case _:
            return f"[{message.role}]: {_trim_summary_source_text(message_text(message))}"


def _format_assistant_summary_source(message: AssistantMessage) -> str:
    parts: list[str] = []
    content = _trim_summary_source_text(message.text)
    if content != "(empty)":
        parts.append(f"[Assistant]: {content}")
    if message.tool_calls:
        calls = [
            f"{call.name}({_format_tool_call_arguments(call.arguments)})"
            for call in message.tool_calls
        ]
        parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
    return "\n".join(parts) if parts else "[Assistant]: (empty)"


def _format_tool_call_arguments(arguments: Mapping[str, object]) -> str:
    return ", ".join(
        f"{key}={json.dumps(value, sort_keys=True)}" for key, value in sorted(arguments.items())
    )


def _trim_summary_source_text(
    text: str,
    *,
    max_chars: int = MAX_SUMMARY_SOURCE_MESSAGE_CHARS,
) -> str:
    normalized = text.strip() or "(empty)"
    if len(normalized) <= max_chars:
        return normalized
    truncated_chars = len(normalized) - max_chars
    return f"{normalized[:max_chars].rstrip()}\n\n[... {truncated_chars} more characters truncated]"


def _branch_file_operations(messages: Sequence[AgentMessage]) -> tuple[list[str], list[str]]:
    read: set[str] = set()
    modified: set[str] = set()
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for call in message.tool_calls:
            path = call.arguments.get("path")
            if not isinstance(path, str) or not path:
                continue
            if call.name == "read":
                read.add(path)
            elif call.name in {"edit", "write"}:
                modified.add(path)
    read_only = sorted(path for path in read if path not in modified)
    return read_only, sorted(modified)
