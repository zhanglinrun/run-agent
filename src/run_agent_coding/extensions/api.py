"""Extension-facing API types and hook payloads."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from run_agent_coding.extensions.disposers import Disposer
from run_agent_coding.host.context_resources import ResourceProvider
from run_agent_coding.host.contracts import HostServices, TaskHandler
from run_agent_core.messages import AgentMessage, CustomMessage, ToolResultMessage
from run_agent_core.tools import AgentTool, AgentToolResult
from run_agent_core.types import JSONValue
from run_agent_observability.sink import ScopedTelemetrySink, TelemetrySink

if TYPE_CHECKING:
    from run_agent_coding.extensions.providers import DynamicProvider
    from run_agent_coding.extensions.runtime import ExtensionRuntime
    from run_agent_coding.paths import RunAgentPaths

AGENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "queue_update",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "compaction_start",
        "compaction_end",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "auto_retry_start",
        "auto_retry_end",
    }
)
AGENT_EVENT_WILDCARD = "agent_event"

LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session_start",
        "session_shutdown",
        "input",
        "before_agent_start",
        "context",
        "tool_call",
        "tool_result",
        "project_trust",
    }
)

SessionLifecycleReason = Literal["startup", "reload", "new", "resume", "branch", "quit"]
DeliverAs = Literal["steer", "follow_up"]
NotifyLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class CustomMessageView:
    """Read-only view of a custom message handed to a message renderer.

    Ports Pi's ``CustomMessage``: ``custom_type`` selects the renderer,
    ``content`` is the LLM-context text, and ``details`` carries arbitrary
    structured data the renderer formats.
    """

    custom_type: str
    content: str
    details: Mapping[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class MessageRenderOptions:
    """Options passed to a message renderer (ports Pi's ``MessageRenderOptions``)."""

    expanded: bool = False


# A message renderer returns a Rich-markup (or plain) string, NOT a Textual
# widget, so extensions never import the TUI toolkit (deviation from Pi's
# ``Component`` return; see the phase-21 custom-renderer Ruling).
MessageRenderer = Callable[[CustomMessageView, MessageRenderOptions], str]

# Host-side resolver installed into render paths: given a custom message's
# fields and whether it is expanded, return rendered markup or ``None`` to fall
# back to the raw content. Errors are swallowed by the resolver, never raised
# into the frontend.
CustomMessageMarkup = Callable[[str, str, "Mapping[str, JSONValue] | None", bool], "str | None"]

# Host-side resolver installed into render paths: given a tool call's name and
# arguments, return the friendly invocation line from the tool's `render_call`
# or ``None`` to fall back to generic formatting. Errors are swallowed by the
# resolver, never raised into the frontend.
ToolCallMarkup = Callable[[str, "Mapping[str, JSONValue]"], "str | None"]

# Host-side resolver installed into render paths: given the tool name, its
# result, and whether the row is expanded, return the display markup from the tool's
# `render_result` or ``None`` to fall back to the generic result block. Errors
# are swallowed by the resolver, never raised into the frontend.
ToolResultMarkup = Callable[[str, AgentToolResult, bool], "str | None"]


class ExtensionError(RuntimeError):
    """Raised when an extension misuses the API (e.g. actions before binding)."""


_STALE_MESSAGE = (
    "extension instance is stale after reload: state captured before /reload"
    " (a saved extension API object, context, or ui handle) must not be reused;"
    " the reloaded extension received a fresh API in its new setup()"
)


class ExtensionGeneration:
    """Liveness token for one extension load generation.

    Ports Pi's ``assertActive``/``invalidate`` staleness guard: every
    :class:`ExtensionAPI` method and every :class:`ExtensionContext`/
    :class:`ExtensionUi` read checks this token before touching the runtime,
    so state captured before a `/reload` fails loudly instead of silently
    acting against the new registration set. Replacement and close invalidate
    captured contexts. Per-source guards share the runtime identity and can
    reject failed setup without invalidating unrelated extensions.
    """

    __slots__ = ("_id", "_parent", "_stale_message")

    def __init__(self, *, parent: ExtensionGeneration | None = None) -> None:
        self._parent = parent
        self._id = parent.id if parent is not None else uuid4().hex
        self._stale_message: str | None = None

    @property
    def id(self) -> str:
        """Return this generation's stable process-local identity."""
        return self._id

    @property
    def active(self) -> bool:
        """Return whether this generation is still the live one."""
        return self._stale_message is None and (self._parent is None or self._parent.active)

    def invalidate(self, message: str | None = None) -> None:
        """Mark this generation stale; the first message wins (Pi parity)."""
        if self._stale_message is None:
            self._stale_message = message or _STALE_MESSAGE

    def assert_active(self) -> None:
        """Raise :class:`ExtensionError` when this generation is stale."""
        if self._parent is not None:
            self._parent.assert_active()
        if self._stale_message is not None:
            raise ExtensionError(self._stale_message)


@dataclass(frozen=True, slots=True)
class TurnStartEvent:
    """Pi-shaped extension event fired at the start of a turn.

    The portable agent event intentionally has no session metadata. The coding
    session adapter adds the zero-based turn index and millisecond timestamp
    before dispatching the event to extensions.
    """

    turn_index: int
    timestamp: int
    type: Literal["turn_start"] = field(default="turn_start", init=False)


@dataclass(frozen=True, slots=True)
class TurnEndEvent:
    """Pi-shaped extension event fired after one assistant/tool turn."""

    turn_index: int
    message: AgentMessage
    tool_results: list[ToolResultMessage]
    type: Literal["turn_end"] = field(default="turn_end", init=False)


@dataclass(frozen=True, slots=True)
class SessionStartEvent:
    """Payload for the `session_start` lifecycle event."""

    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class SessionShutdownEvent:
    """Payload for the `session_shutdown` lifecycle event."""

    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Payload for the `input` hook: raw user prompt text, before expansion.

    Mirrors Pi's `InputEvent`. `source` says where the input came from:
    ``"interactive"`` for TUI/print-mode user input, ``"extension"`` for a turn
    an extension started via ``send_user_message``/``send_custom_message``.
    `streaming_behavior` says how the input will be queued when the agent is
    mid-run (``"steer"``/``"follow_up"``), and is ``None`` on the idle prompt
    path.

    Pi's `images` field is omitted (Run Agent has no image input yet) and Pi's
    ``"rpc"`` source is omitted (Run Agent has no RPC mode). Both defaults keep
    existing handlers that read only ``.text`` working unchanged.
    """

    text: str
    source: Literal["interactive", "extension"] = "interactive"
    streaming_behavior: Literal["steer", "follow_up"] | None = None


@dataclass(frozen=True, slots=True)
class InputHookResult:
    """Result of an `input` hook handler.

    `action="continue"` leaves the text unchanged, `"transform"` replaces it
    with `text` (transforms chain across handlers), and `"handled"` consumes
    the input entirely, optionally showing `message` to the user.
    """

    action: Literal["continue", "transform", "handled"] = "continue"
    text: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class BeforeAgentStartEvent:
    """Expanded prompt and base system prompt for one new agent run."""

    prompt: str
    system_prompt: str
    type: Literal["before_agent_start"] = field(default="before_agent_start", init=False)


@dataclass(frozen=True, slots=True)
class BeforeAgentStartResult:
    """Add durable custom messages or override the system prompt for this run only."""

    messages: tuple[CustomMessage, ...] = ()
    system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ContextEvent:
    """Detached message snapshot immediately before a model request."""

    messages: tuple[AgentMessage, ...]
    type: Literal["context"] = field(default="context", init=False)


@dataclass(frozen=True, slots=True)
class ContextHookResult:
    """Replace request messages without rewriting the durable session transcript."""

    messages: Sequence[AgentMessage]


@dataclass(frozen=True, slots=True)
class ToolCallHookEvent:
    """Prepared arguments and call identity, before a tool executes."""

    tool_name: str
    arguments: Mapping[str, JSONValue]
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class ToolCallHookResult:
    """Result of a `tool_call` hook handler.

    Set `block=True` (with an optional `reason`) to prevent execution, or
    return replacement `arguments` to rewrite the call. Blocking wins over
    argument rewrites and short-circuits remaining handlers.
    """

    block: bool = False
    reason: str | None = None
    arguments: Mapping[str, JSONValue] | None = None
    terminate: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultHookEvent:
    """Payload for the `tool_result` hook, after a tool executes."""

    tool_name: str
    arguments: Mapping[str, JSONValue]
    result: AgentToolResult
    tool_call_id: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class ToolResultHookResult:
    """Result of a `tool_result` hook handler; set fields to override."""

    content: str | None = None
    details: dict[str, JSONValue] | None = None
    is_error: bool | None = None
    terminate: bool | None = None


ExtensionHandler = Callable[[object, "ExtensionContext"], object | Awaitable[object]]
# Command handlers are sync-only: the slash-command path (CommandRegistry ->
# CodingSession.handle_command -> TUI submit) is synchronous end to end.
ExtensionCommandHandler = Callable[
    ["str", "ExtensionCommandContext"], "str | None | Awaitable[str | None]"
]


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeDiagnostic:
    """A runtime failure raised by an extension handler."""

    extension: str
    event: str
    message: str


class UiBridge(Protocol):
    """Host-provided UI capabilities available to extensions.

    Dialog methods (`select`/`confirm`/`input`) are async and mirror Pi's
    `ctx.ui`. Without an interactive frontend they return the Pi no-op
    defaults (`None`/`False`/`None`). `timeout` (seconds) auto-dismisses a
    dialog with the no-op default; `None` waits indefinitely.
    """

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached."""
        ...

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification to the user (no-op without a UI)."""
        ...

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Show a picker; return the chosen option, or None on cancel."""
        ...

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Show a confirmation; return True only if confirmed."""
        ...

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Show a text prompt; return the entered text, or None on cancel."""
        ...

    def set_status(self, source: str, key: str, text: str | None) -> None: ...

    def clear_status(self, source: str | None = None) -> None: ...


class NullUiBridge:
    """UI bridge used when no interactive frontend is attached."""

    @property
    def has_ui(self) -> bool:
        """Return False: print mode has no interactive UI."""
        return False

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Ignore notifications without a UI."""

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Return None: no UI to pick from (Pi no-op default)."""
        return None

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Return False: no UI to confirm with (Pi no-op default)."""
        return False

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Return None: no UI to enter text into (Pi no-op default)."""
        return None

    def set_status(self, source: str, key: str, text: str | None) -> None:
        """Headless hosts do not render status lines."""

    def clear_status(self, source: str | None = None) -> None:
        """No status is retained by headless hosts."""


class StderrUiBridge(NullUiBridge):
    """UI bridge that writes extension notifications to stderr (print mode).

    Inherits the Pi no-op dialog defaults from `NullUiBridge`; only
    `notify` is observable in print mode.
    """

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Print the notification to stderr."""
        print(f"[extension:{level}] {message}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class ExtensionCommandContext:
    """Context passed to extension slash-command handlers."""

    name: str
    args: str
    api: ExtensionAPI


class ExtensionUi:
    """Interactive UI facade exposed to extensions as `context.ui`.

    Mirrors Pi's `ctx.ui`: async `select`/`confirm`/`input` dialogs plus a
    synchronous `notify`. Every call delegates to the host UI bridge, which
    returns the Pi no-op defaults when no interactive frontend is attached.
    Every member (trivial reads included, matching Pi) asserts the owning
    load generation is still active and raises :class:`ExtensionError` when
    the facade was captured before a `/reload`.
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        source_id: str = "",
    ) -> None:
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._source_id = source_id

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached."""
        self._generation.assert_active()
        return self._runtime.ui.has_ui

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Prompt the user to pick an option; None on cancel/no UI."""
        self._generation.assert_active()
        return await self._runtime.ui.select(title, options, timeout=timeout)

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Ask the user to confirm; True only if confirmed."""
        self._generation.assert_active()
        return await self._runtime.ui.confirm(title, message, timeout=timeout)

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Prompt the user for text; None on cancel/no UI."""
        self._generation.assert_active()
        return await self._runtime.ui.input(title, placeholder, secret=secret, timeout=timeout)

    def set_status(self, key: str, text: str | None) -> None:
        """Update this extension's terminal status without owning UI widgets."""
        self._generation.assert_active()
        if not key.strip():
            raise ExtensionError("Status key must not be empty")
        self._runtime.ui.set_status(self._source_id, key, text)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification in the UI, if one is attached."""
        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


class ExtensionContext:
    """Read-only session context exposed to extensions.

    Every property (trivial reads included, matching Pi's context getters)
    asserts the owning load generation is still active, so a context captured
    before a `/reload` raises :class:`ExtensionError` instead of reading the
    reloaded world.
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        source_id: str = "",
    ) -> None:
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._source_id = source_id
        self._ui = ExtensionUi(runtime, self._generation, source_id=source_id)

    @property
    def cwd(self) -> Path:
        """Return the session working directory."""
        self._generation.assert_active()
        return self._runtime.session_view.cwd

    @property
    def telemetry(self) -> TelemetrySink:
        """The host's observation sink; extensions do not open database connections."""
        self._generation.assert_active()
        session = self._runtime.session_view
        prefix = sha256(f"{session.session_id}\0{self._source_id}".encode()).hexdigest() + ":"
        return ScopedTelemetrySink(session.telemetry, prefix, self._generation.assert_active)

    @property
    def services(self) -> HostServices:
        self._generation.assert_active()
        return self._runtime.host_services_for_source(self._source_id)

    @property
    def paths(self) -> RunAgentPaths:
        """Return canonical host storage paths for extension-owned artifacts."""
        self._generation.assert_active()
        return self._runtime.paths

    @property
    def environment(self) -> Mapping[str, str]:
        """Return the immutable environment snapshot captured for this runtime."""
        self._generation.assert_active()
        return self._runtime.environment

    @property
    def model(self) -> str:
        """Return the active model name."""
        self._generation.assert_active()
        return self._runtime.session_view.model

    @property
    def provider_name(self) -> str:
        """Return the active provider name."""
        self._generation.assert_active()
        return self._runtime.session_view.provider_name

    @property
    def inference_provider(self) -> str | None:
        """Return the active Hugging Face inference-provider pin, if any."""
        self._generation.assert_active()
        return self._runtime.session_view.inference_provider

    @property
    def inference_provider_mode(self) -> str:
        """Return whether Hugging Face routing is automatic or explicitly fixed."""
        self._generation.assert_active()
        return self._runtime.session_view.inference_provider_mode

    @property
    def session_id(self) -> str | None:
        """Return the current session id, if the session is indexed."""
        self._generation.assert_active()
        return self._runtime.session_view.session_id

    @property
    def session_name(self) -> str | None:
        """Return the session's human-friendly name, if it has one."""
        self._generation.assert_active()
        return self._runtime.session_view.session_name

    @property
    def thinking_level(self) -> str:
        """Return the active thinking mode for future turns."""
        self._generation.assert_active()
        return self._runtime.session_view.thinking_level

    @property
    def system_prompt(self) -> str:
        """Return the active system prompt."""
        self._generation.assert_active()
        return self._runtime.session_view.system_prompt

    @property
    def is_running(self) -> bool:
        """Return whether an agent run is currently active."""
        self._generation.assert_active()
        return self._runtime.session_view.is_running

    @property
    def transcript(self) -> tuple[AgentMessage, ...]:
        """Return the active-path parent conversation as read-only copies.

        Mirrors the read access Pi extensions get via
        ``ctx.sessionManager.getBranch()``: the user/assistant/tool messages on
        the current branch, with compaction and branch summaries already folded
        in as ``UserMessage`` entries (Run Agent has no separate summary message
        type). Each message is deep-copied so an extension mutating a returned
        object cannot corrupt the live session transcript.
        """
        self._generation.assert_active()
        messages = self._runtime.session_view.messages
        return tuple(message.model_copy(deep=True) for message in messages)

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached."""
        self._generation.assert_active()
        return self._runtime.ui.has_ui

    @property
    def ui(self) -> ExtensionUi:
        """Return the interactive UI facade (Pi's `ctx.ui`).

        Use `await context.ui.select/confirm/input(...)` to drive dialogs.
        Because command handlers are sync (see the docs), a `/command` that
        needs a dialog should spawn a loop task that awaits `context.ui`.
        """
        self._generation.assert_active()
        return self._ui


class ExtensionAPI:
    """The object handed to each extension's ``setup(api)`` entry point.

    Every method and property asserts the load generation first (Pi's
    ``assertActive`` parity): after `/reload` replaces the registration set,
    an API object captured by the previous instance raises
    :class:`ExtensionError` on any use instead of silently acting against
    the new world.
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        extension_name: str,
        generation: ExtensionGeneration | None = None,
        *,
        source_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._extension_name = extension_name
        self._source_id = source_id or f"extension-name:{extension_name}"
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._context = ExtensionContext(
            runtime,
            self._generation,
            source_id=self._source_id,
        )

    @property
    def name(self) -> str:
        """Return this extension's name."""
        self._generation.assert_active()
        return self._extension_name

    @property
    def context(self) -> ExtensionContext:
        """Return read-only session context."""
        self._generation.assert_active()
        return self._context

    def register_task_handler(self, name: str, handler: TaskHandler) -> None:
        self._generation.assert_active()
        self._runtime.register_task_handler(self._source_id, name, handler)

    def register_resource_provider(
        self, name: str, provider: ResourceProvider, *, version: str
    ) -> None:
        """Select versioned context from a read-only, host-scoped resource view."""
        self._generation.assert_active()
        self._runtime.register_resource_provider(self._source_id, name, version, provider)

    def register_disposer(self, disposer: Disposer) -> None:
        """Own an async cleanup callback until setup rollback, reload or close."""
        self._generation.assert_active()
        self._runtime.register_disposer(self._source_id, disposer)

    def set_inference_provider(self, route: str | None) -> str:
        """Select or reset the active Hugging Face session route."""
        self._generation.assert_active()
        return self._runtime.session_view.set_inference_provider(route)

    def register_tool(self, tool: AgentTool) -> None:
        """Register an agent tool (first registration per name wins)."""
        self._generation.assert_active()
        self._runtime.register_tool(self._source_id, self._extension_name, tool)

    def register_provider(self, provider: DynamicProvider) -> None:
        """Register or atomically replace this source's dynamic provider layer."""
        self._generation.assert_active()
        self._runtime.register_provider(self._source_id, provider)

    def update_provider(self, provider: DynamicProvider) -> bool:
        """Update this source's provider snapshot while preserving its layer token."""
        self._generation.assert_active()
        return self._runtime.update_provider(self._source_id, provider)

    def register_command(
        self,
        name: str,
        handler: ExtensionCommandHandler,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Register a slash command backed by this extension."""
        self._generation.assert_active()
        self._runtime.register_command(
            self._source_id,
            self._extension_name,
            name,
            handler,
            description=description,
            usage=usage,
            aliases=aliases,
        )

    def add_prompt_guideline(self, guideline: str) -> None:
        """Add a standalone guideline line to the system prompt.

        Tool-attached guidance belongs on the tool (`prompt_snippet`,
        `prompt_guidelines`); this is for behavioral guidance not tied to
        any tool. Duplicate lines are de-duplicated at prompt build time.
        """
        self._generation.assert_active()
        self._runtime.register_prompt_guideline(self._source_id, self._extension_name, guideline)

    def add_prompt_section(self, title: str | None, body: str) -> None:
        """Append a free-form, optionally titled section to the system prompt.

        Use this for structured, always-on extension context such as procedures,
        paragraphs, and code blocks. Use :meth:`add_prompt_guideline` for one
        behavioral bullet instead.
        """
        self._generation.assert_active()
        self._runtime.register_prompt_section(
            self._source_id,
            self._extension_name,
            title,
            body,
        )

    def on(
        self,
        event: str,
        handler: ExtensionHandler | None = None,
    ) -> Callable[[ExtensionHandler], ExtensionHandler] | ExtensionHandler:
        """Subscribe to an event, directly or as a decorator."""
        self._generation.assert_active()
        if handler is not None:
            self._runtime.subscribe(self._source_id, event, handler)
            return handler

        def decorator(decorated: ExtensionHandler) -> ExtensionHandler:
            self._generation.assert_active()
            self._runtime.subscribe(self._source_id, event, decorated)
            return decorated

        return decorator

    def send_user_message(
        self,
        content: str,
        *,
        deliver_as: DeliverAs = "follow_up",
    ) -> None:
        """Queue a user message for the active or next agent run."""
        self._generation.assert_active()
        self._runtime.send_user_message(content, deliver_as=deliver_as)

    def register_message_renderer(
        self,
        custom_type: str,
        renderer: MessageRenderer,
    ) -> None:
        """Register a renderer for custom messages with this ``custom_type``.

        Ports Pi's ``registerMessageRenderer``: the first registration per
        ``custom_type`` wins. The renderer receives a :class:`CustomMessageView`
        and :class:`MessageRenderOptions` and returns a Rich-markup string; it
        must not return a Textual widget (that keeps extensions TUI-free).
        """
        self._generation.assert_active()
        self._runtime.register_message_renderer(
            self._source_id, self._extension_name, custom_type, renderer
        )

    def send_custom_message(
        self,
        content: str,
        *,
        custom_type: str,
        details: dict[str, JSONValue] | None = None,
        deliver_as: DeliverAs = "follow_up",
        trigger_turn: bool = True,
    ) -> None:
        """Send a custom message that renders via a registered renderer.

        Ports Pi's ``sendMessage``: ``content`` still enters LLM context, while
        ``custom_type``/``details`` let a registered renderer format the
        transcript block. With ``trigger_turn`` (the default) the message starts
        a turn when the session is idle, mirroring ``send_user_message``; set it
        to ``False`` to only queue for the next run.
        """
        self._generation.assert_active()
        self._runtime.send_custom_message(
            content,
            custom_type=custom_type,
            details=details,
            deliver_as=deliver_as,
            trigger_turn=trigger_turn,
        )

    async def append_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:
        """Persist extension-owned data to the session as a custom entry."""
        self._generation.assert_active()
        await self._runtime.append_custom_entry(namespace, data)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification in the UI, if one is attached."""
        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


@dataclass(slots=True)
class RegisteredExtension:
    """Book-keeping for one loaded extension inside the runtime."""

    name: str
    source_id: str
    path: Path | None
    api: ExtensionAPI
    source: Literal["user", "explicit", "project"] = "explicit"
    handlers: dict[str, list[ExtensionHandler]] = field(default_factory=dict)
    code_version: str | None = None
    package_dir: Path | None = None
