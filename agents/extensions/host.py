"""Ordered extension host and AgentCore adapters."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
import inspect
import json
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..runtime.contracts import ToolCall, ToolResult
from ..runtime.hooks import ModelContext, NextTurnDecision, ToolCallDecision, TurnResult
from ..tools.schema import ToolValidationError, validate_tool_input
from .contracts import (
    CommandRegistration,
    ExtensionAPI,
    ExtensionContext,
    ExtensionEvent,
    ExtensionSpec,
    PromptContribution,
    PromptRenderContext,
    SourceInfo,
    ToolHandlerResult,
    ToolRegistration,
)


class ExtensionLoadError(RuntimeError):
    pass


class ExtensionHost:
    """Load trusted extensions and project their capabilities into one task."""

    def __init__(self, specs: Iterable[ExtensionSpec]) -> None:
        self.specs = self._sort_specs(tuple(specs))
        self._handlers: dict[str, list[tuple[SourceInfo, Any]]] = defaultdict(list)
        self._tools: dict[str, ToolRegistration] = {}
        self._commands: dict[str, CommandRegistration] = {}
        self._prompt_contributions: dict[str, PromptContribution] = {}
        self._services: dict[str, tuple[SourceInfo, Any]] = {}
        self._execution_factory: tuple[SourceInfo, Any] | None = None
        self._active_tools: set[str] = set()
        self._loaded = False
        self._root_context: ExtensionContext | None = None
        self._context: ContextVar[ExtensionContext | None] = ContextVar(
            f"run_agent_extension_context_{id(self)}", default=None
        )

    @staticmethod
    def _sort_specs(specs: tuple[ExtensionSpec, ...]) -> tuple[ExtensionSpec, ...]:
        by_name: dict[str, ExtensionSpec] = {}
        order: dict[str, int] = {}
        for index, spec in enumerate(specs):
            if not spec.name or spec.name in by_name:
                raise ExtensionLoadError(f"duplicate or empty extension name: {spec.name!r}")
            by_name[spec.name] = spec
            order[spec.name] = index
        missing = {
            requirement
            for spec in specs
            for requirement in spec.requires
            if requirement not in by_name
        }
        if missing:
            raise ExtensionLoadError(
                "missing extension dependencies: " + ", ".join(sorted(missing))
            )

        pending = set(by_name)
        resolved: list[ExtensionSpec] = []
        resolved_names: set[str] = set()
        while pending:
            ready = sorted(
                (
                    name
                    for name in pending
                    if set(by_name[name].requires).issubset(resolved_names)
                ),
                key=order.__getitem__,
            )
            if not ready:
                cycle = ", ".join(sorted(pending))
                raise ExtensionLoadError(f"extension dependency cycle: {cycle}")
            for name in ready:
                pending.remove(name)
                resolved_names.add(name)
                resolved.append(by_name[name])
        return tuple(resolved)

    def load(self) -> None:
        if self._loaded:
            return
        for spec in self.specs:
            snapshot = self._snapshot()
            try:
                if inspect.iscoroutinefunction(spec.setup):
                    raise TypeError("extension setup(api) must be synchronous")
                result = spec.setup(ExtensionAPI(self, spec.source_info()))
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    raise TypeError("extension setup(api) must not return an awaitable")
            except Exception as exc:
                self._restore(snapshot)
                raise ExtensionLoadError(
                    f"failed to load extension {spec.name}: {type(exc).__name__}: {exc}"
                ) from exc
        self._loaded = True

    def _snapshot(self) -> tuple[Any, ...]:
        return (
            {name: list(items) for name, items in self._handlers.items()},
            dict(self._tools),
            dict(self._commands),
            dict(self._prompt_contributions),
            dict(self._services),
            self._execution_factory,
            set(self._active_tools),
        )

    def _restore(self, snapshot: tuple[Any, ...]) -> None:
        handlers, tools, commands, prompts, services, execution, active = snapshot
        self._handlers = defaultdict(list, handlers)
        self._tools = tools
        self._commands = commands
        self._prompt_contributions = prompts
        self._services = services
        self._execution_factory = execution
        self._active_tools = active

    def register_handler(self, event: str, handler: Any, source: SourceInfo) -> None:
        if not event or not callable(handler):
            raise ValueError("event name and callable handler are required")
        self._handlers[event].append((source, handler))

    def register_tool(self, registration: ToolRegistration, *, replace: bool = False) -> None:
        name = registration.name
        if not name:
            raise ValueError("tool definition requires a non-empty name")
        if not isinstance(registration.definition.get("input_schema"), dict):
            raise ValueError(f"tool {name} requires an input_schema object")
        if name in self._tools and not replace:
            owner = self._tools[name].source.name
            raise ValueError(f"tool {name!r} already registered by {owner}")
        self._tools[name] = registration
        if not registration.deferred:
            self._active_tools.add(name)

    def register_command(self, registration: CommandRegistration, *, replace: bool = False) -> None:
        if not registration.name:
            raise ValueError("command name is required")
        if registration.name in self._commands and not replace:
            owner = self._commands[registration.name].source.name
            raise ValueError(f"command {registration.name!r} already registered by {owner}")
        self._commands[registration.name] = registration

    def register_prompt_contribution(self, contribution: PromptContribution) -> None:
        if not contribution.id:
            raise ValueError("prompt contribution id is required")
        if contribution.id in self._prompt_contributions:
            owner = self._prompt_contributions[contribution.id].source.name
            raise ValueError(
                f"prompt contribution {contribution.id!r} already registered by {owner}"
            )
        self._prompt_contributions[contribution.id] = contribution

    def provide_service(
        self,
        name: str,
        service: Any,
        source: SourceInfo,
        *,
        replace: bool = False,
    ) -> None:
        if name in self._services and not replace:
            owner = self._services[name][0].name
            raise ValueError(f"service {name!r} already provided by {owner}")
        self._services[name] = (source, service)
        if self._root_context is not None:
            self._root_context.services[name] = service

    def register_execution_factory(
        self,
        factory: Any,
        source: SourceInfo,
        *,
        replace: bool = False,
    ) -> None:
        if self._execution_factory is not None and not replace:
            owner = self._execution_factory[0].name
            raise ValueError(f"execution factory already registered by {owner}")
        self._execution_factory = (source, factory)

    async def create_execution(self, task: Any, workspace: Path) -> Any:
        if self._execution_factory is None:
            raise RuntimeError("no extension registered an execution factory")
        value = self._execution_factory[1](task, workspace)
        return await value if inspect.isawaitable(value) else value

    def bind(self, context: ExtensionContext) -> None:
        self.load()
        context.host = self
        context.services.update({name: value for name, (_source, value) in self._services.items()})
        self._root_context = context

    @property
    def current_context(self) -> ExtensionContext:
        context = self._context.get() or self._root_context
        if context is None:
            raise RuntimeError("extension host is not bound to a task")
        return context

    @contextmanager
    def use_context(self, context: ExtensionContext) -> Iterator[ExtensionContext]:
        context.host = self
        token = self._context.set(context)
        try:
            yield context
        finally:
            self._context.reset(token)

    def get_active_tools(self) -> list[str]:
        return sorted(self._active_tools)

    def set_active_tools(self, names: Iterable[str]) -> None:
        requested = {str(name) for name in names}
        unknown = requested - set(self._tools)
        if unknown:
            raise ValueError("unknown tools: " + ", ".join(sorted(unknown)))
        self._active_tools = requested

    def activate_tools(self, names: Iterable[str]) -> None:
        requested = {str(name) for name in names}
        unknown = requested - set(self._tools)
        if unknown:
            raise ValueError("unknown tools: " + ", ".join(sorted(unknown)))
        self._active_tools.update(requested)

    def registered_tool_names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def _tool_names_for(self, context: ExtensionContext | None = None) -> set[str]:
        context = context or self.current_context
        names = (
            set(context.active_tool_names)
            if context.active_tool_names is not None
            else set(self._active_tools)
        )
        if context.tool_ceiling_names is not None:
            names &= set(context.tool_ceiling_names)
        return names

    def tool_definitions(self, context: ExtensionContext | None = None) -> list[dict[str, Any]]:
        names = self._tool_names_for(context)
        return [dict(registration.definition) for name, registration in self._tools.items() if name in names]

    def tool_registration(self, name: str) -> ToolRegistration | None:
        return self._tools.get(name)

    def search_tools(
        self,
        query: str,
        context: ExtensionContext | None = None,
    ) -> list[dict[str, Any]]:
        context = context or self.current_context
        text = str(query or "").lower()
        active = self._tool_names_for(context)
        ceiling = (
            set(context.tool_ceiling_names)
            if context.tool_ceiling_names is not None
            else set(self._tools)
        )
        matches: list[ToolRegistration] = []
        for registration in self._tools.values():
            if (
                not registration.deferred
                or registration.name in active
                or registration.name not in ceiling
            ):
                continue
            haystack = f"{registration.name} {registration.definition.get('description', '')}".lower()
            if text in haystack:
                matches.append(registration)
        names = {item.name for item in matches}
        if context.active_tool_names is None:
            self.activate_tools(names)
        else:
            context.active_tool_names = frozenset(active | names)
        return [dict(item.definition) for item in matches]

    def commands(self) -> tuple[CommandRegistration, ...]:
        return tuple(self._commands.values())

    async def dispatch_command(self, name: str, args: str = "") -> Any:
        command = self._commands.get(name)
        if command is None:
            raise KeyError(name)
        value = command.handler(args, self.current_context)
        return await value if inspect.isawaitable(value) else value

    async def emit(self, event_name: str, **data: Any) -> ExtensionEvent:
        event = ExtensionEvent(event_name, dict(data))
        for _source, handler in tuple(self._handlers.get(event_name, ())):
            value = handler(event, self.current_context)
            if inspect.isawaitable(value):
                await value
        return event

    async def transform_context(self, context: ModelContext) -> ModelContext:
        extension_context = self.current_context
        definitions = self.tool_definitions(extension_context)
        messages = list(context.messages)
        latest_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user" and isinstance(message.get("content"), str)
            ),
            extension_context.task.prompt,
        )
        prompt_context = PromptRenderContext(
            extension_context.workspace,
            tuple(messages),
            tuple(definitions),
            latest_user,
        )
        sections = [extension_context.base_prompt]
        snippets: list[str] = []
        guidelines: list[str] = []
        active_names = self._tool_names_for(extension_context)
        for name, registration in self._tools.items():
            if name not in active_names:
                continue
            if registration.prompt_snippet:
                snippets.append(f"- {registration.prompt_snippet}")
            guidelines.extend(registration.prompt_guidelines)
        if snippets:
            sections.append("# Available tools\n" + "\n".join(snippets))
        if guidelines:
            sections.append("# Tool guidelines\n" + "\n".join(f"- {item}" for item in guidelines))
        contributions = sorted(
            self._prompt_contributions.values(),
            key=lambda item: (item.priority, item.id),
        )
        for contribution in contributions:
            value = contribution.render(prompt_context, extension_context)
            if inspect.isawaitable(value):
                value = await value
            text = str(value or "").strip()
            if text:
                sections.append(text)
        current = ModelContext(
            "\n\n".join(section.strip() for section in sections if section.strip()),
            messages,
            definitions,
        )
        event = ExtensionEvent("context", {"context": current})
        for _source, handler in tuple(self._handlers.get("context", ())):
            value = handler(event, extension_context)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ModelContext):
                current = value
                event.data["context"] = current
            elif isinstance(event.data.get("context"), ModelContext):
                current = event.data["context"]
        return current

    async def before_tool_call(self, call: ToolCall) -> ToolCallDecision:
        context = self.current_context
        if call.name not in self._tool_names_for(context):
            return ToolCallDecision("deny", f"Tool is not active in this context: {call.name}")
        try:
            validate_tool_input(call.name, call.input, self.tool_definitions(context))
        except ToolValidationError as exc:
            return ToolCallDecision("deny", str(exc))
        event = ExtensionEvent("tool_call", {"call": call})
        current = call
        for _source, handler in tuple(self._handlers.get("tool_call", ())):
            value = handler(event, context)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ToolCallDecision):
                if value.action != "allow":
                    return value
                if value.input is not None:
                    current = ToolCall(current.id, current.name, dict(value.input))
                    event.data["call"] = current
            mutated = event.data.get("call")
            if isinstance(mutated, ToolCall):
                current = mutated
            if current.name != call.name or current.id != call.id:
                return ToolCallDecision("deny", "Tool name and call id are immutable.")
            try:
                validate_tool_input(current.name, current.input, self.tool_definitions(context))
            except ToolValidationError as exc:
                return ToolCallDecision("deny", str(exc))
        authorizer = context.services.get("authorizer")
        if authorizer is not None:
            decision = authorizer(current.name, current.input, context, current.id)
            if inspect.isawaitable(decision):
                decision = await decision
            if isinstance(decision, ToolCallDecision):
                if decision.action != "allow":
                    return decision
                if decision.input is not None:
                    current = ToolCall(current.id, current.name, dict(decision.input))
                    try:
                        validate_tool_input(
                            current.name,
                            current.input,
                            self.tool_definitions(context),
                        )
                    except ToolValidationError as exc:
                        return ToolCallDecision("deny", str(exc))
        return ToolCallDecision(input=current.input if current.input != call.input else None)

    async def after_tool_call(self, result: ToolResult) -> ToolResult:
        event = ExtensionEvent("tool_result", {"result": result})
        current = result
        for _source, handler in reversed(tuple(self._handlers.get("tool_result", ()))):
            value = handler(event, self.current_context)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ToolResult):
                current = value
                event.data["result"] = current
            elif isinstance(event.data.get("result"), ToolResult):
                current = event.data["result"]
        return current

    async def should_stop_after_turn(self, turn: TurnResult) -> bool:
        event = ExtensionEvent("should_stop", {"turn": turn})
        stop = False
        for _source, handler in tuple(self._handlers.get("should_stop", ())):
            value = handler(event, self.current_context)
            if inspect.isawaitable(value):
                value = await value
            stop = bool(value) or stop
        return stop

    async def prepare_next_turn(self, turn: TurnResult) -> NextTurnDecision:
        event = ExtensionEvent("prepare_next_turn", {"turn": turn})
        message: dict[str, Any] | None = None
        for _source, handler in tuple(self._handlers.get("prepare_next_turn", ())):
            value = handler(event, self.current_context)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, NextTurnDecision):
                if not value.continue_run:
                    return value
                if value.message is not None:
                    if message is not None:
                        raise RuntimeError("multiple extensions produced next-turn messages")
                    message = value.message
        return NextTurnDecision(True, message)


class ExtensionToolExecutor:
    """Execute only active registered tools after the extension guard chain."""

    def __init__(self, host: ExtensionHost, *, allowed_names: Iterable[str] | None = None) -> None:
        self.host = host
        self.allowed_names = set(allowed_names) if allowed_names is not None else None

    async def execute(self, call: ToolCall) -> ToolResult:
        context = self.host.current_context
        if self.allowed_names is not None and call.name not in self.allowed_names:
            return ToolResult(
                call.id,
                call.name,
                f"Tool is not available in this delegated context: {call.name}",
                False,
                error="tool_not_available",
                executed=False,
            )
        registration = self.host.tool_registration(call.name)
        if registration is None or call.name not in self.host._tool_names_for(context):
            return ToolResult(
                call.id,
                call.name,
                f"Unknown or inactive tool: {call.name}",
                False,
                error="unknown_tool",
                executed=False,
            )
        try:
            normalized = validate_tool_input(
                call.name, call.input, self.host.tool_definitions(context)
            )
        except ToolValidationError as exc:
            return ToolResult(
                call.id,
                call.name,
                str(exc),
                False,
                error="schema_validation",
                executed=False,
            )

        started = time.perf_counter()
        try:
            value = registration.handler(normalized, context)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, ToolHandlerResult):
                content = value.content
                ok = value.ok
                error = value.error
            elif isinstance(value, str):
                content = value
                ok = True
                error = None
            elif value is None:
                content = ""
                ok = True
                error = None
            else:
                content = json.dumps(value, ensure_ascii=False, default=str)
                ok = True
                error = None
            return ToolResult(
                call.id,
                call.name,
                content,
                ok,
                (time.perf_counter() - started) * 1000,
                error,
            )
        except Exception as exc:
            return ToolResult(
                call.id,
                call.name,
                f"Error: {exc}",
                False,
                (time.perf_counter() - started) * 1000,
                str(exc),
            )


__all__ = ["ExtensionHost", "ExtensionLoadError", "ExtensionToolExecutor"]
