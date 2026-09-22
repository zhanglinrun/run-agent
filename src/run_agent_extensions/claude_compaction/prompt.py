# ruff: noqa: E501
# The prompt text below is copied verbatim from the reference implementation
# (`prompt.ts`), so its line breaks and lengths are part of the port.
"""Summarization prompt, continuation text and summary cleanup.

A 1:1 port of ``prompt.ts``: the no-tools preamble, the base compact prompt with
its ``<analysis>/<summary>`` scaffolding, the continuation message that carries
``the summary below covers the earlier portion``, the transcript path, and
``Recent messages are preserved verbatim.``, plus ``formatCompactSummary`` which
strips the analysis scratchpad and rewrites the summary tags.
"""

from __future__ import annotations

import re

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn — you will fail the task.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

DETAILED_ANALYSIS_INSTRUCTION_BASE = """Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
   - Note any security-relevant instructions or constraints the user stated (e.g., sensitive files or data to avoid, operations that must not be performed, credential or secret handling rules). These MUST be preserved verbatim in the summary so they continue to apply after compaction.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly."""

BASE_COMPACT_PROMPT = f"""Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

{DETAILED_ANALYSIS_INSTRUCTION_BASE}

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent. Preserve any security-relevant instructions or constraints verbatim so they remain in effect after compaction. Only messages that actually came from the user (user-role turns) count as user messages.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Important Code Snippet]

4. Errors and fixes:
   - [Detailed description of error 1]:
      - [How you fixed the error]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]

7. Pending Tasks:
   - [Task 1]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.
"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)

_ANALYSIS_PATTERN = re.compile(r"<analysis>[\s\S]*?</analysis>")
_SUMMARY_PATTERN = re.compile(r"<summary>([\s\S]*?)</summary>")
_BLANK_LINE_PATTERN = re.compile(r"\n\n+")


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Return the summarization prompt, with optional extra instructions."""
    prompt = NO_TOOLS_PREAMBLE + BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip() != "":
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    return prompt + NO_TOOLS_TRAILER


def format_compact_summary(summary: str) -> str:
    """Strip the ``<analysis>`` scratchpad and rewrite ``<summary>`` as a header."""
    formatted = _ANALYSIS_PATTERN.sub("", summary, count=1)
    match = _SUMMARY_PATTERN.search(formatted)
    if match:
        content = match.group(1) or ""
        formatted = _SUMMARY_PATTERN.sub(f"Summary:\n{content.strip()}", formatted, count=1)
    formatted = _BLANK_LINE_PATTERN.sub("\n\n", formatted)
    return formatted.strip()


def get_compact_user_summary_message(
    summary: str,
    suppress_follow_up_questions: bool = False,
    transcript_path: str | None = None,
    recent_messages_preserved: bool = False,
) -> str:
    """Return the continuation message that carries a compaction summary."""
    formatted = format_compact_summary(summary)
    base_summary = (
        "This session is being continued from a previous conversation that ran out of "
        "context. The summary below covers the earlier portion of the conversation.\n\n"
        f"{formatted}"
    )
    if transcript_path:
        base_summary += (
            "\n\nIf you need specific details from before compaction (like exact code "
            f"snippets, error messages, or content you generated), read the full transcript at: {transcript_path}"
        )
    if recent_messages_preserved:
        base_summary += "\n\nRecent messages are preserved verbatim."
    if suppress_follow_up_questions:
        return (
            f"{base_summary}\nContinue the conversation from where it left off without asking "
            "the user any further questions. Resume directly — do not acknowledge the summary, "
            'do not recap what was happening, do not preface with "I\'ll continue" or similar. '
            "Pick up the last task as if the break never happened."
        )
    return base_summary


__all__ = [
    "BASE_COMPACT_PROMPT",
    "DETAILED_ANALYSIS_INSTRUCTION_BASE",
    "NO_TOOLS_PREAMBLE",
    "NO_TOOLS_TRAILER",
    "format_compact_summary",
    "get_compact_prompt",
    "get_compact_user_summary_message",
]
