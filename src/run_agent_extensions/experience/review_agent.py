"""The bounded tool loop a background review or a curator pass runs in.

hermes-agent forks a whole ``AIAgent`` for its background review: the fork replays the
conversation, gets the review instruction as its user message, and calls the memory
and skill tools through the normal tool loop, capped at 16 iterations and an aggregate
input-token budget. This host exposes a model to an extension only through
``HostServices.inference.complete``: one frozen request and one response. The review
selects only the memory and skill tool schemas; the host returns proposed calls without
executing them. Each iteration replays the conversation plus the tool exchange, and
the extension applies calls under the review origin. A bounded JSON-call prefix is
also accepted for providers that return text instead of native tool calls.

Cancellation follows hermes' ``_BackgroundReviewRun``: a live user turn asks the review
to stop and waits a bounded time for the acknowledgement; the loop checks between
iterations, and a completion refused with ``InferenceBusy`` (the host's own "a
foreground run is in flight") ends the review as superseded rather than retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from run_agent_coding.host.inference import (
    InferenceBusy,
    InferenceRequest,
    InferenceResult,
    InferenceService,
    InferenceUnavailable,
)
from run_agent_coding.thinking import ThinkingLevel
from run_agent_core.tools import AgentToolResult
from run_agent_core.types import JSONValue

from .review_models import REVIEW_DENIED_TOOL
from .worker import ReviewLedger

logger = logging.getLogger(__name__)

ToolExecutor = Callable[[str, Mapping[str, JSONValue]], Awaitable[AgentToolResult]]

# Roughly four characters per token: the estimate hermes uses when a provider reports
# nothing, applied here only to preflight the cumulative input budget. It is not
# provider-reported usage or an exact tokenizer measurement.
CHARS_PER_TOKEN = 4
TOOL_RESULT_PREVIEW_CHARS = 4_000
TRANSCRIPT_TOOL_PREVIEW_CHARS = 400

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)


@dataclass(slots=True)
class ReviewRun:
    """Per-review cancellation and request-completion handshake (hermes' run token)."""

    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    request_done: asyncio.Event = field(default_factory=asyncio.Event)
    label: str = "review"

    @property
    def active(self) -> bool:
        return not self.request_done.is_set()

    def finish(self) -> None:
        self.request_done.set()

    async def cancel_and_wait(self, timeout: float) -> bool:
        """Ask the review to stop and wait up to ``timeout`` for it; True when it did.

        Foreground priority is preserved: a review that does not acknowledge in time is
        left to notice the cancellation on its own, and the live turn proceeds anyway.
        """
        self.cancel_requested.set()
        if self.request_done.is_set():
            return True
        try:
            await asyncio.wait_for(self.request_done.wait(), timeout=max(timeout, 0.0))
        except TimeoutError:
            logger.warning(
                "%s did not acknowledge cancellation within %.1fs; proceeding with the live turn",
                self.label,
                timeout,
            )
            return False
        return True


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call the loop executed and what came back."""

    tool: str
    arguments: dict[str, JSONValue]
    result_text: str
    accepted: bool
    details: dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str
    calls: tuple[ToolCallRecord, ...]
    iterations: int
    stop_reason: str
    error: str | None = None

    @property
    def accepted_calls(self) -> tuple[ToolCallRecord, ...]:
        return tuple(call for call in self.calls if call.accepted)

    @property
    def refused_calls(self) -> tuple[ToolCallRecord, ...]:
        return tuple(call for call in self.calls if not call.accepted)


@dataclass(frozen=True, slots=True)
class ParsedToolCall:
    tool: str
    arguments: dict[str, JSONValue]


def parse_tool_call(text: str) -> ParsedToolCall | None:
    """Read one ``{"tool": ..., "args": {...}}`` answer; anything else is a final reply.

    A code fence around the object is tolerated; prose around it is not, so a summary
    that happens to mention a tool is never mistaken for a call.
    """
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    tool = decoded.get("tool") or decoded.get("name")
    args = decoded.get("args", decoded.get("arguments", {}))
    if not isinstance(tool, str) or not tool.strip():
        return None
    if not isinstance(args, dict):
        return None
    return ParsedToolCall(tool.strip(), {str(k): v for k, v in args.items()})


def _parse_tool_reply(text: str) -> tuple[tuple[ParsedToolCall, ...], str] | None:
    """Accept a JSON-call prefix and optional summary, never calls embedded in prose.

    Some providers emit several JSON objects in one completion despite the single-call
    instruction. Validate the entire prefix before executing any of it.
    """
    remaining = text.strip()
    fenced = _FENCE.match(remaining)
    if fenced:
        remaining = fenced.group(1).strip()
    parsed: list[ParsedToolCall] = []
    decoder = json.JSONDecoder()
    while remaining.startswith("{"):
        try:
            _, end = decoder.raw_decode(remaining)
        except json.JSONDecodeError:
            return None
        call = parse_tool_call(remaining[:end])
        if call is None:
            return None
        parsed.append(call)
        remaining = remaining[end:].strip()
    return (tuple(parsed), remaining) if parsed else None


def denied_tool_result(tool_name: str) -> AgentToolResult:
    """hermes' runtime denial for a tool outside the review whitelist."""
    from run_agent_core.messages import TextContent

    return AgentToolResult(
        content=[TextContent(text=REVIEW_DENIED_TOOL.format(tool_name=tool_name))],
        details={"accepted": False, "denied": True},
    )


def render_transcript(messages: Sequence[object], *, char_budget: int) -> str:
    """Replay a conversation as role-labelled lines, keeping the tail that fits."""
    lines = [rendered for message in messages if (rendered := render_message(message))]
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        used += len(line) + 1
        if used > char_budget:
            kept.append("[earlier conversation omitted to fit the review budget]")
            break
        kept.append(line)
    return "\n".join(reversed(kept))


def render_message(message: object) -> str:
    """One transcript message as hermes' digest renders it: role, tools, text."""
    data: Mapping[str, Any]
    if isinstance(message, Mapping):
        data = message
    elif hasattr(message, "model_dump"):
        dumped = message.model_dump(mode="json")
        data = dumped if isinstance(dumped, Mapping) else {}
    else:
        return ""
    role = str(data.get("role") or "?")
    parts: list[str] = []
    tools: list[str] = []
    content = data.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, Mapping):
                continue
            kind = part.get("type")
            if kind == "text":
                parts.append(str(part.get("text") or ""))
            elif kind == "toolCall":
                tools.append(str(part.get("name") or "?"))
    error = data.get("error_message") or data.get("errorMessage")
    text = " ".join(part.strip() for part in parts if part and part.strip()).strip()
    if role == "toolResult":
        name = data.get("tool_name") or data.get("toolName") or "?"
        preview = text[:TRANSCRIPT_TOOL_PREVIEW_CHARS]
        if len(text) > TRANSCRIPT_TOOL_PREVIEW_CHARS:
            preview += "…"
        return f"TOOL({name}): {preview}" if preview else f"TOOL({name}): (no output)"
    label = "USER" if role in {"user", "custom"} else role.upper()
    out: list[str] = []
    if tools:
        out.append(f"{label}[tools: {', '.join(tools)}]")
    if text:
        out.append(f"{label}: {text}")
    if error:
        out.append(f"{label}[error: {error}]")
    return "\n".join(out)


async def run_tool_loop(
    *,
    inference: InferenceService,
    system: str,
    conversation: str,
    instruction: str,
    execute: ToolExecutor,
    ledger: ReviewLedger,
    purpose: str,
    run: ReviewRun | None = None,
    max_iterations: int = 16,
    max_input_tokens: int = 0,
    finish_run: bool = True,
    thinking_level: ThinkingLevel | None = None,
    max_output_tokens: int | None = None,
    tool_names: tuple[str, ...] = (),
) -> LoopOutcome:
    """Drive one bounded agentic pass over ``inference``.

    Every iteration sends ``conversation`` + ``instruction`` + the exchange so far as
    the prompt; native or JSON tool calls are executed and their results appended.
    A final text response ends the loop. Stops on the iteration cap, input budget, a
    cancellation, or a busy host.
    """
    calls: list[ToolCallRecord] = []
    exchange: list[str] = []
    iterations = 0
    stop_reason = "final answer"
    final_text = ""
    error: str | None = None
    try:
        while True:
            if run is not None and run.cancel_requested.is_set():
                stop_reason = "superseded by a new live turn"
                break
            if iterations >= max_iterations or ledger.exhausted:
                stop_reason = "iteration budget exhausted"
                break
            prompt = _compose_prompt(conversation, instruction, exchange)
            estimate = max(1, (len(system) + len(prompt) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)
            caps = [cap for cap in (max_input_tokens, ledger.budget.max_input_tokens) if cap > 0]
            charged_input = max(ledger.input_tokens, ledger.estimated_input_tokens)
            if caps and charged_input + estimate > min(caps):
                stop_reason = "input budget exhausted"
                break
            try:
                iterations += 1
                ledger.begin_request(estimate)
                result = await _complete_or_cancel(
                    inference,
                    InferenceRequest(
                        prompt=prompt,
                        system=system,
                        purpose=purpose,
                        thinking_level=thinking_level,
                        max_output_tokens=max_output_tokens,
                        tool_names=tool_names,
                    ),
                    run,
                )
            except InferenceBusy:
                stop_reason = "superseded by a new live turn"
                break
            except InferenceUnavailable as exc:
                stop_reason = "no provider"
                error = f"{type(exc).__name__}: {exc}"
                break
            if result is None:
                stop_reason = "superseded by a new live turn"
                break
            ledger.record_usage(result.input_tokens, result.output_tokens)
            if run is not None and run.cancel_requested.is_set():
                stop_reason = "superseded by a new live turn"
                break
            reply = (
                (
                    tuple(
                        ParsedToolCall(call.name, dict(call.arguments))
                        for call in result.tool_calls
                    ),
                    "",
                )
                if result.tool_calls
                else _parse_tool_reply(result.text)
            )
            if reply is None:
                final_text = result.text.strip()
                break
            parsed_calls, summary = reply
            tool_cap = min(max_iterations, ledger.budget.max_model_requests)
            if len(calls) + len(parsed_calls) > tool_cap:
                stop_reason = "tool call budget exhausted"
                error = f"review tool calls exceed the configured limit of {tool_cap}"
                break
            for parsed in parsed_calls:
                if run is not None and run.cancel_requested.is_set():
                    break
                outcome = await execute(parsed.tool, parsed.arguments)
                record = ToolCallRecord(
                    tool=parsed.tool,
                    arguments=dict(parsed.arguments),
                    result_text=outcome.text,
                    accepted=bool(_details(outcome).get("accepted", True)),
                    details=dict(_details(outcome)),
                )
                calls.append(record)
                exchange.append(
                    "ASSISTANT (tool call): "
                    + json.dumps(
                        {"tool": parsed.tool, "args": parsed.arguments}, ensure_ascii=False
                    )
                )
                preview = outcome.text[:TOOL_RESULT_PREVIEW_CHARS]
                if len(outcome.text) > TOOL_RESULT_PREVIEW_CHARS:
                    preview += "…"
                exchange.append(f"TOOL RESULT ({parsed.tool}): {preview}")
            if run is not None and run.cancel_requested.is_set():
                stop_reason = "superseded by a new live turn"
                break
            if summary:
                final_text = summary
                break
    except asyncio.CancelledError:
        if run is not None:
            run.cancel_requested.set()
        raise
    except Exception as exc:  # retain diagnostic failures for the coordinator
        stop_reason = "error"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if run is not None and finish_run:
            run.finish()
    return LoopOutcome(
        final_text=final_text,
        calls=tuple(calls),
        iterations=iterations,
        stop_reason=stop_reason,
        error=error,
    )


async def _complete_or_cancel(
    inference: InferenceService, request: InferenceRequest, run: ReviewRun | None
) -> InferenceResult | None:
    """One completion that a live turn can interrupt; ``None`` when it was cancelled.

    hermes interrupts the forked agent mid-request; here the in-flight completion is
    cancelled locally. The remote provider may continue computing after disconnect.
    """
    if run is None:
        return await inference.complete(request)
    completion = asyncio.ensure_future(inference.complete(request))
    cancelled = asyncio.ensure_future(run.cancel_requested.wait())
    try:
        done, _ = await asyncio.wait({completion, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if completion in done:
            return completion.result()
        completion.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await completion
        return None
    finally:
        if not completion.done():
            completion.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await completion
        if not cancelled.done():
            cancelled.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancelled


def _compose_prompt(conversation: str, instruction: str, exchange: Sequence[str]) -> str:
    blocks = [
        "=== Conversation under review (oldest first) ===",
        conversation or "(no conversation was recorded)",
        "=== End of conversation ===",
        "",
        instruction,
    ]
    if exchange:
        blocks.append("")
        blocks.append("=== Your review so far ===")
        blocks.extend(exchange)
        blocks.append(
            "Continue: reply with exactly one more JSON tool call, or with your final "
            "plain-text summary."
        )
    return "\n".join(blocks)


def _details(result: AgentToolResult) -> Mapping[str, JSONValue]:
    details = result.details
    return details if isinstance(details, Mapping) else {}


# -- action summaries (hermes summarize_background_review_actions) ----------------------


def summarize_actions(calls: Sequence[ToolCallRecord], *, mode: str = "on") -> list[str]:
    """Human-facing lines for the memory and skill writes a pass actually made.

    ``mode`` follows hermes' ``display.memory_notifications``: ``off`` yields nothing,
    ``on`` generic "Memory updated" / tool messages, ``verbose`` compact previews of the
    written content.
    """
    mode = (mode or "on").lower()
    if mode == "off":
        return []
    verbose = mode == "verbose"
    actions: list[str] = []
    for call in calls:
        if not call.accepted or call.details.get("changed") is False:
            continue
        args = call.arguments
        message = call.result_text.splitlines()[0] if call.result_text else ""
        if call.tool == "skill_manage":
            action = str(args.get("action") or "")
            if action in {"list", "view"}:
                continue
            name = str(args.get("name") or "")
            if not verbose:
                actions.append(message or f"Skill {action}")
                continue
            if action == "patch":
                old = str(args.get("old_text") or "")
                new = str(args.get("new_text") or "")
                actions.append(
                    f'📝 Skill \'{name}\' patched: "{_preview(old, 80)}" → "{_preview(new, 80)}"'
                )
            elif action == "create":
                description = str(args.get("description") or "")
                created = f"📝 Skill '{name}' created: {description}"
                actions.append(created if description else message)
            elif action == "edit":
                actions.append(f"📝 Skill '{name}' rewritten")
            else:
                actions.append(f"📝 {message}" if message else f"Skill {action}")
            continue
        if call.tool != "memory":
            continue
        target = str(args.get("target") or "memory")
        label = "Memory" if target == "memory" else "User profile" if target == "user" else target
        if not verbose:
            actions.append(f"{label} updated")
            continue
        operations = args.get("operations")
        ops: list[Mapping[str, JSONValue]] = (
            [op for op in operations if isinstance(op, Mapping)]
            if isinstance(operations, list)
            else []
        )
        if not ops:
            ops = [args]
        for op in ops:
            op_action = str(op.get("action") or "")
            content = str(op.get("content") or op.get("new_content") or op.get("new_text") or "")
            old_text = str(op.get("old_text") or "")
            if op_action == "add" and content:
                actions.append(f"{label} ➕ {_preview(content, 120)}")
            elif op_action == "replace" and content:
                actions.append(f"{label} ✏️ {_preview(content, 120)}")
            elif op_action == "remove" and old_text:
                actions.append(f"{label} ➖ {_preview(old_text, 60)}")
            else:
                actions.append(f"{label} updated")
    # Preserve order, drop exact repeats (hermes joins ``dict.fromkeys(actions)``).
    return list(dict.fromkeys(actions))


def _preview(text: str, width: int) -> str:
    flat = text.replace("\n", " ")
    return flat[:width] + ("…" if len(flat) > width else "")


__all__ = [
    "CHARS_PER_TOKEN",
    "LoopOutcome",
    "ParsedToolCall",
    "ReviewRun",
    "ToolCallRecord",
    "ToolExecutor",
    "denied_tool_result",
    "parse_tool_call",
    "render_message",
    "render_transcript",
    "run_tool_loop",
    "summarize_actions",
]
