"""Pi-compatible provider-neutral tool definitions and execution results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, model_validator

from run_agent_core.messages import ImageContent, TextContent, ToolCall, Usage, WireModel
from run_agent_core.types import JSONValue


class ToolCancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether tool execution should stop."""
        ...


class AgentToolResult(WireModel):
    """Final or partial result produced by a tool."""

    content: list[TextContent | ImageContent] = Field(default_factory=list)
    details: JSONValue = None
    usage: Usage | None = None
    """Model usage a tool spent on its own (a sub-agent, a summariser), charged to the turn."""
    added_tool_names: list[str] | None = None
    terminate: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_text_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class ToolCallRenderer(Protocol):
    def __call__(self, arguments: Mapping[str, JSONValue]) -> str | None:
        """Return a frontend-friendly tool invocation, or ``None``."""
        ...


class ToolResultRenderer(Protocol):
    def __call__(self, result: AgentToolResult, *, expanded: bool) -> str | None:
        """Return frontend markup for a tool result, or ``None``."""
        ...


ToolUpdateCallback = Callable[[AgentToolResult], None]


class ToolExecutor(Protocol):
    def __call__(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]:
        """Execute one validated tool call."""
        ...


ToolExecutionMode = Literal["sequential", "parallel"]
ToolArgumentPreparer = Callable[[object], Mapping[str, JSONValue]]


@dataclass(frozen=True, slots=True)
class AgentTool:
    """A tool exposed to the portable agent loop."""

    name: str
    label: str
    description: str
    parameters: Mapping[str, JSONValue]
    execute_fn: ToolExecutor
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    prepare_arguments: ToolArgumentPreparer | None = None
    execution_mode: ToolExecutionMode = "parallel"
    replay: Literal["never", "safe"] | None = None
    """Whether a run interrupted mid-call may re-execute this tool on resume.

    ``"safe"`` marks an idempotent tool; ``"never"`` (and the default) means the
    interrupted call is recorded as such rather than re-run.
    """
    render_call: ToolCallRenderer | None = None
    render_result: ToolResultRenderer | None = None

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        """Alias used by provider payload builders."""
        return self.parameters

    async def execute(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Execute a tool with Pi-compatible call-id and progress semantics."""
        return await self.execute_fn(tool_call_id, arguments, signal, on_update)


def validate_tool_arguments(
    schema: Mapping[str, JSONValue], arguments: Mapping[str, JSONValue]
) -> dict[str, JSONValue]:
    """Check arguments against a tool's JSON schema, coercing the obvious cases.

    Ports the shape of Pi's ``validateToolArguments``: required keys must be
    present, declared property types are enforced, and a scalar that arrived as
    a string (``"3"`` for an integer, ``"true"`` for a boolean) is converted so
    a model quoting a number does not fail the call. Unknown keys are kept; the
    schema is a contract for what is required, not a filter.
    """
    properties = schema.get("properties")
    required = schema.get("required")
    checked: dict[str, JSONValue] = dict(arguments)
    if isinstance(required, list):
        missing = [name for name in required if isinstance(name, str) and name not in checked]
        if missing:
            raise ValueError(f"Missing required argument(s): {', '.join(missing)}")
    if not isinstance(properties, Mapping):
        return checked
    for name, spec in properties.items():
        if name not in checked or not isinstance(spec, Mapping):
            continue
        value = checked[name]
        if value is None:
            continue
        expected = spec.get("type")
        if isinstance(expected, list):
            expected = next((item for item in expected if item != "null"), None)
        if not isinstance(expected, str):
            continue
        checked[name] = _coerce_argument(name, value, expected)
    return checked


def _coerce_argument(name: str, value: JSONValue, expected: str) -> JSONValue:
    if expected == "string":
        if isinstance(value, str):
            return value
    elif expected == "integer":
        if isinstance(value, bool):
            pass
        elif isinstance(value, int):
            return value
        elif isinstance(value, float) and value.is_integer():
            return int(value)
        elif isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
    elif expected == "number":
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            return value
        elif isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
    elif expected == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
    elif expected == "array":
        if isinstance(value, list):
            return value
    elif expected == "object":
        if isinstance(value, dict):
            return value
    else:
        return value
    raise ValueError(f"Argument {name!r} must be of type {expected}, got {type(value).__name__}")


__all__ = [
    "AgentTool",
    "AgentToolResult",
    "ToolCall",
    "ToolCallRenderer",
    "ToolCancellationToken",
    "ToolExecutionMode",
    "ToolResultRenderer",
    "ToolExecutor",
    "ToolUpdateCallback",
    "validate_tool_arguments",
]
