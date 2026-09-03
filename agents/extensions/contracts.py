"""Public contracts for Run Agent's Python extension system."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from ..providers.base import ProviderAdapter
    from ..runtime.contracts import ToolCall
    from ..runtime.hooks import ModelContext
    from ..runtime.tracing import TraceRecorder
    from ..session import SessionRepository
    from ..harness.task import TaskSpec, TaskState


MaybeAwaitable = Any | Awaitable[Any]
ExtensionScope = Literal["builtin", "user", "project", "explicit", "inline"]
EXTENSION_API_VERSION = 1
ToolHandler = Callable[[dict[str, Any], "ExtensionContext"], MaybeAwaitable]
EventHandler = Callable[["ExtensionEvent", "ExtensionContext"], MaybeAwaitable]
CommandHandler = Callable[[str, "ExtensionContext"], MaybeAwaitable]
PromptRenderer = Callable[["PromptRenderContext", "ExtensionContext"], MaybeAwaitable]
ExecutionFactory = Callable[["TaskSpec", Path], MaybeAwaitable]
ExtensionSetup = Callable[["ExtensionAPI"], None]


@dataclass(frozen=True)
class SourceInfo:
    """Origin metadata attached to every contributed capability."""

    name: str
    scope: ExtensionScope = "inline"
    path: Path | None = None


@dataclass(frozen=True)
class ExtensionSpec:
    """One extension factory and its explicit dependency contract."""

    name: str
    setup: ExtensionSetup
    requires: tuple[str, ...] = ()
    source: SourceInfo | None = None

    def source_info(self) -> SourceInfo:
        return self.source or SourceInfo(self.name)


@dataclass
class ExtensionEvent:
    """Typed envelope used by the ordered extension event chain."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRegistration:
    """A model-visible JSON Schema tool plus its runtime handler."""

    definition: dict[str, Any]
    handler: ToolHandler
    source: SourceInfo
    prompt_snippet: str = ""
    prompt_guidelines: tuple[str, ...] = ()
    deferred: bool = False

    @property
    def name(self) -> str:
        return str(self.definition.get("name") or "")


@dataclass(frozen=True)
class CommandRegistration:
    name: str
    handler: CommandHandler
    source: SourceInfo
    description: str = ""


@dataclass(frozen=True)
class PromptContribution:
    """A named prompt fragment rebuilt from immutable base state each turn."""

    id: str
    render: PromptRenderer
    source: SourceInfo
    priority: int = 100


@dataclass(frozen=True)
class PromptRenderContext:
    workspace: Path
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    latest_user_message: str


@dataclass(frozen=True)
class ToolHandlerResult:
    """Structured result returned by an extension tool handler."""

    content: str
    ok: bool = True
    error: str | None = None


@dataclass
class RunOutcome:
    """Mutable task outcome passed through after_solve/after_run extensions."""

    final_text: str = ""
    patch: str = ""
    report: Any | None = None
    acceptance: Any | None = None
    failure: Any | None = None


@dataclass
class ExtensionContext:
    """Task-scoped runtime context exposed to trusted extensions."""

    task: "TaskSpec"
    state: "TaskState"
    repository: "SessionRepository"
    journal: Any
    provider: "ProviderAdapter"
    execution: Any
    artifact_root: Path
    trace: "TraceRecorder"
    base_prompt: str = ""
    services: dict[str, Any] = field(default_factory=dict)
    outcome: RunOutcome = field(default_factory=RunOutcome)
    active_tool_names: frozenset[str] | None = None
    tool_ceiling_names: frozenset[str] | None = None
    depth: int = 0
    host: Any = field(default=None, repr=False)

    @property
    def workspace(self) -> Path:
        return self.state.workspace

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        if not replace and name in self.services:
            raise ValueError(f"service already provided: {name}")
        self.services[name] = value

    def require(self, name: str) -> Any:
        if name not in self.services:
            raise RuntimeError(f"required extension service is unavailable: {name}")
        return self.services[name]

    def append_state(self, extension: str, data: Any) -> None:
        from ..session import EntryType

        latest = self.repository.latest_entry(self.state.session_id, lane_id=self.state.lane_id)
        self.repository.append_entry(
            self.state.session_id,
            self.state.lane_id,
            EntryType.CUSTOM,
            {"extension": extension, "data": data},
            parent_id=latest.id if latest else None,
        )

    def latest_state(self, extension: str) -> Any | None:
        for entry in reversed(self.repository.list_branch(self.state.session_id, lane_id=self.state.lane_id)):
            if entry.type.value == "custom" and entry.payload.get("extension") == extension:
                return entry.payload.get("data")
        return None

    async def authorize(self, name: str, value: dict[str, Any], *, call_id: str = "extension") -> Any:
        authorizer = self.require("authorizer")
        return await _await(authorizer(name, value, self, call_id))

    async def side_query(self, system: str, user_message: str) -> str:
        from ..providers import ModelRequest

        phase = "repair" if self.state.phase.value == "correcting" else "solve"
        self.state.budgets.ensure_available()
        context_digest = hashlib.sha256(
            f"{system}\0{user_message}".encode("utf-8")
        ).hexdigest()
        self.trace.emit(
            "model.request",
            side_query=True,
            phase=phase,
            context_digest=context_digest,
            message_count=1,
            tool_names=[],
        )
        response = await self.provider.complete(
            ModelRequest(({"role": "user", "content": user_message},), (), system)
        )
        self.trace.emit(
            "model.response",
            side_query=True,
            phase=phase,
            text=response.text,
            tool_count=0,
            stop_reason=response.stop_reason,
            usage=response.usage,
        )
        self.state.budgets.consume_usage(
            input_tokens=int(response.usage.get("input", response.usage.get("prompt_tokens", 0)) or 0),
            output_tokens=int(response.usage.get("output", response.usage.get("completion_tokens", 0)) or 0),
        )
        return response.text


async def _await(value: MaybeAwaitable) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class ExtensionAPI:
    """Registration surface passed to extension setup functions."""

    def __init__(self, host: Any, source: SourceInfo) -> None:
        self._host = host
        self.source = source
        self.api_version = EXTENSION_API_VERSION

    def on(self, event: str, handler: EventHandler) -> None:
        self._host.register_handler(event, handler, self.source)

    def register_tool(
        self,
        definition: dict[str, Any],
        handler: ToolHandler,
        *,
        prompt_snippet: str = "",
        prompt_guidelines: tuple[str, ...] | list[str] = (),
        deferred: bool = False,
        replace: bool = False,
    ) -> None:
        self._host.register_tool(
            ToolRegistration(
                dict(definition),
                handler,
                self.source,
                prompt_snippet,
                tuple(prompt_guidelines),
                deferred,
            ),
            replace=replace,
        )

    def register_command(
        self,
        name: str,
        handler: CommandHandler,
        *,
        description: str = "",
        replace: bool = False,
    ) -> None:
        self._host.register_command(
            CommandRegistration(name, handler, self.source, description),
            replace=replace,
        )

    def contribute_prompt(
        self,
        contribution_id: str,
        render: PromptRenderer,
        *,
        priority: int = 100,
    ) -> None:
        self._host.register_prompt_contribution(
            PromptContribution(contribution_id, render, self.source, priority)
        )

    def provide(self, name: str, service: Any, *, replace: bool = False) -> None:
        self._host.provide_service(name, service, self.source, replace=replace)

    def register_execution_factory(self, factory: ExecutionFactory, *, replace: bool = False) -> None:
        self._host.register_execution_factory(factory, self.source, replace=replace)

    def get_active_tools(self) -> list[str]:
        return self._host.get_active_tools()

    def set_active_tools(self, names: list[str] | tuple[str, ...] | set[str]) -> None:
        self._host.set_active_tools(names)

    def activate_tools(self, names: list[str] | tuple[str, ...] | set[str]) -> None:
        self._host.activate_tools(names)

    def append_entry(self, custom_type: str, data: Any = None) -> None:
        context = self._host.current_context
        context.append_state(custom_type, data)


__all__ = [
    "CommandRegistration",
    "ExtensionAPI",
    "ExtensionContext",
    "EXTENSION_API_VERSION",
    "ExtensionEvent",
    "ExtensionScope",
    "ExtensionSpec",
    "PromptContribution",
    "PromptRenderContext",
    "RunOutcome",
    "SourceInfo",
    "ToolRegistration",
]
