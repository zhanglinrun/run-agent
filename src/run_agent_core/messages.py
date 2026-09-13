"""Pi-compatible provider-neutral content and transcript message models."""

from __future__ import annotations

from collections.abc import Sequence
from time import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from run_agent_core.types import JSONValue


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def current_timestamp_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""
    return int(time() * 1000)


class WireModel(BaseModel):
    """Strict model with Python field names and Pi-compatible JSON aliases."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        alias_generator=_to_camel,
    )


class UsageCost(WireModel):
    """Billed response cost in USD."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class Usage(WireModel):
    """Provider-reported token usage for one assistant response."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int | None = None
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = UsageCost()


class ResponseTiming(WireModel):
    """Monotonic request durations for one assistant response."""

    time_to_first_output_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int = Field(ge=0)


class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str
    text_signature: str | None = None


class ThinkingContent(WireModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False


class ImageContent(WireModel):
    type: Literal["image"] = "image"
    data: str
    mime_type: str


class ToolCall(WireModel):
    """A tool call content block requested by the assistant."""

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)
    thought_signature: str | None = None


type UserContent = str | list[TextContent | ImageContent]
type AssistantContent = TextContent | ThinkingContent | ToolCall
type ToolResultContent = TextContent | ImageContent


class UserMessage(WireModel):
    role: Literal["user"] = "user"
    content: UserContent
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return content_text(self.content)


class AssistantDiagnosticError(WireModel):
    name: str | None = None
    message: str
    stack: str | None = None
    code: str | int | None = None


class AssistantMessageDiagnostic(WireModel):
    type: str
    timestamp: int = Field(default_factory=current_timestamp_ms)
    error: AssistantDiagnosticError | None = None
    details: dict[str, JSONValue] | None = None


StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


class AssistantMessage(WireModel):
    """A Pi-compatible assistant message with ordered content blocks."""

    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    api: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"
    response_model: str | None = None
    response_id: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage = Usage()
    timing: ResponseTiming | None = None
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        """Accept a string only as a Python construction convenience.

        The stored model and serialized protocol are always block based. This
        keeps provider and test construction concise without creating a second
        message representation.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        usage = data.get("usage")
        if usage is None:
            data["usage"] = Usage()
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    @property
    def thinking_text(self) -> str:
        return "".join(
            block.thinking for block in self.content if isinstance(block, ThinkingContent)
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCall))


class ToolResultMessage(WireModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent] = Field(default_factory=list)
    details: JSONValue = None
    usage: Usage | None = None
    added_tool_names: list[str] | None = None
    is_error: bool = False
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return content_text(self.content)


class BashExecutionMessage(WireModel):
    role: Literal["bashExecution"] = "bashExecution"
    command: str
    output: str
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)
    exclude_from_context: bool = False


class CustomMessage(WireModel):
    role: Literal["custom"] = "custom"
    custom_type: str
    content: UserContent
    display: bool = True
    details: JSONValue = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return content_text(self.content)


class BranchSummaryMessage(WireModel):
    role: Literal["branchSummary"] = "branchSummary"
    summary: str
    from_id: str
    timestamp: int = Field(default_factory=current_timestamp_ms)


class CompactionSummaryMessage(WireModel):
    role: Literal["compactionSummary"] = "compactionSummary"
    summary: str
    tokens_before: int
    timestamp: int = Field(default_factory=current_timestamp_ms)


type AgentMessage = Annotated[
    UserMessage
    | AssistantMessage
    | ToolResultMessage
    | BashExecutionMessage
    | CustomMessage
    | BranchSummaryMessage
    | CompactionSummaryMessage,
    Field(discriminator="role"),
]


def assistant_content(
    text: str,
    tool_calls: list[ToolCall] | tuple[ToolCall, ...] = (),
) -> list[AssistantContent]:
    """Build canonical ordered assistant blocks from parser accumulators."""
    blocks: list[AssistantContent] = [TextContent(text=text)] if text else []
    blocks.extend(tool_calls)
    return blocks


def content_text(content: str | list[Any]) -> str:
    """Return visible text from string or text/image content."""
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextContent))


COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the following "
    "summary:\n\n<summary>\n"
)
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"
BRANCH_SUMMARY_PREFIX = (
    "The following is a summary of a branch that this conversation came back from:\n\n<summary>\n"
)
BRANCH_SUMMARY_SUFFIX = "</summary>"

type LlmMessage = UserMessage | AssistantMessage | ToolResultMessage


def bash_execution_to_text(message: BashExecutionMessage) -> str:
    """Render a user-run shell command the way Pi shows it to the model."""
    text = f"Ran `{message.command}`\n"
    text += f"```\n{message.output}\n```" if message.output else "(no output)"
    if message.cancelled:
        text += "\n\n(command cancelled)"
    elif message.exit_code not in (None, 0):
        text += f"\n\nCommand exited with code {message.exit_code}"
    if message.truncated and message.full_output_path:
        text += f"\n\n[Output truncated. Full output: {message.full_output_path}]"
    return text


def message_to_user(message: AgentMessage) -> UserMessage:
    """Convert one session-only message to the user message the provider sees.

    Ports Pi's ``convertToLlm``: summaries are wrapped so the model knows they
    are summaries, shell executions become a fenced transcript, and custom
    messages keep their content blocks. Provider-native roles pass through.
    """
    if isinstance(message, UserMessage):
        return message
    if isinstance(message, BashExecutionMessage):
        return UserMessage(content=bash_execution_to_text(message), timestamp=message.timestamp)
    if isinstance(message, CustomMessage):
        return UserMessage(content=message.content, timestamp=message.timestamp)
    if isinstance(message, BranchSummaryMessage):
        text = f"{BRANCH_SUMMARY_PREFIX}{message.summary}{BRANCH_SUMMARY_SUFFIX}"
        return UserMessage(content=text, timestamp=message.timestamp)
    if isinstance(message, CompactionSummaryMessage):
        text = f"{COMPACTION_SUMMARY_PREFIX}{message.summary}{COMPACTION_SUMMARY_SUFFIX}"
        return UserMessage(content=text, timestamp=message.timestamp)
    return UserMessage(content=message_text(message), timestamp=message.timestamp)


def convert_to_llm(messages: Sequence[AgentMessage]) -> list[LlmMessage]:
    """Project a transcript onto the three roles a provider accepts.

    A shell execution marked ``exclude_from_context`` is dropped, as in Pi.
    """
    converted: list[LlmMessage] = []
    for message in messages:
        if isinstance(message, (AssistantMessage, ToolResultMessage)):
            converted.append(message)
        elif isinstance(message, BashExecutionMessage) and message.exclude_from_context:
            continue
        else:
            converted.append(message_to_user(message))
    return converted


def message_text(message: AgentMessage) -> str:
    """Return the user-visible text represented by an agent message."""
    if isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage, CustomMessage)):
        return message.text
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return message.summary
    if isinstance(message, BashExecutionMessage):
        return message.output
    return ""
