"""Extension-facing API types and hook payloads."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar
from uuid import uuid4

from run_agent_coding.events import CompactionReason
from run_agent_coding.extensions.disposers import Disposer
from run_agent_coding.host.context_resources import ExtensionResourceSnapshot, ResourceProvider
from run_agent_coding.host.contracts import HostServices, TaskHandler
from run_agent_core.messages import AgentMessage, CustomMessage, ToolResultMessage
from run_agent_core.provider import ModelRequest
from run_agent_core.tools import AgentTool, AgentToolResult
from run_agent_core.types import JSONValue
from run_agent_observability.sink import ScopedTelemetrySink, TelemetrySink

if TYPE_CHECKING:
    from run_agent_coding.extensions.providers import DynamicProvider
    from run_agent_coding.extensions.runtime import ExtensionRuntime
    from run_agent_coding.paths import RunAgentPaths
    from run_agent_coding.skills import Skill

OBSERVATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "session_start",
        "session_shutdown",
        "session_info_changed",
        "session_compact",
        "session_compact_failed",
        "session_tree",
        "after_provider_response",
        "model_select",
        "thinking_level_select",
        "ui_prompt_start",
        "ui_prompt_end",
    }
)
HOOK_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "input",
        "before_agent_start",
        "context",
        "tool_call",
        "tool_result",
        "project_trust",
        "resources_discover",
        "session_before_switch",
        "session_before_fork",
        "session_before_compact",
        "session_before_tree",
        "before_provider_headers",
        "before_provider_request",
        "user_bash",
        "message_end",
    }
)
# Aliases: observation names used to live in AGENT_EVENT_TYPES, hooks in LIFECYCLE.
AGENT_EVENT_TYPES = OBSERVATION_EVENT_TYPES
LIFECYCLE_EVENT_TYPES = HOOK_EVENT_TYPES
AGENT_EVENT_WILDCARD = "agent_event"
EXTENSION_EVENT_TYPES = OBSERVATION_EVENT_TYPES | HOOK_EVENT_TYPES

SessionLifecycleReason = Literal["startup", "reload", "new", "resume", "branch", "quit"]
DeliverAs = Literal["steer", "follow_up"]
NotifyLevel = Literal["info", "warning", "error"]
_T = TypeVar("_T")


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


# A message renderer returns a Rich-markup (or plain) string rather than a
# widget, so extensions never import the terminal toolkit (Pi returns a
# ``Component`` here).
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

    ``retiring`` is a committed, read-only notification phase: captured contexts
    may still inspect immutable session metadata, while every API, UI, task, and
    host-service mutation is rejected. ``retired`` rejects both reads and writes.
    Child generations inherit the strictest state from their parent.
    """

    __slots__ = ("_id", "_parent", "_state", "_stale_message")

    def __init__(self, *, parent: ExtensionGeneration | None = None) -> None:
        self._parent = parent
        self._id = parent.id if parent is not None else uuid4().hex
        self._state: Literal["active", "retiring", "retired"] = "active"
        self._stale_message: str | None = None

    @property
    def id(self) -> str:
        """Return this generation's stable process-local identity."""
        return self._id

    @property
    def state(self) -> Literal["active", "retiring", "retired"]:
        if self._parent is not None and self._parent.state != "active":
            return self._parent.state
        return self._state

    @property
    def active(self) -> bool:
        """Return whether this generation can still mutate runtime state."""
        return self.state == "active"

    def begin_retiring(self) -> None:
        """Enter the committed read-only shutdown phase."""
        if self._state == "active":
            self._state = "retiring"

    def invalidate(self, message: str | None = None) -> None:
        """Retire this generation; the first diagnostic message wins."""
        self._state = "retired"
        if self._stale_message is None:
            self._stale_message = message or _STALE_MESSAGE

    def assert_readable(self) -> None:
        """Allow immutable reads during shutdown, but never after retirement."""
        if self._parent is not None:
            self._parent.assert_readable()
        if self._state == "retired":
            raise ExtensionError(self._stale_message or _STALE_MESSAGE)

    def assert_active(self) -> None:
        """Raise unless this generation still owns mutation authority."""
        if self._parent is not None:
            self._parent.assert_active()
        if self._state != "active":
            raise ExtensionError(self._stale_message or _STALE_MESSAGE)


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
class SessionShutdownContext:
    """Minimal, frozen read-only context for the `session_shutdown` notification.

    Shutdown handlers receive only the reason, the session identity and the
    working directory. The full `ExtensionContext` surface (host services,
    state, tasks, UI, paths, telemetry) is intentionally absent, so a handler
    cannot hold mutation authority while the observed runtime retires.
    """

    reason: SessionLifecycleReason
    session_id: str | None
    cwd: Path


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Payload for the `input` hook: raw user prompt text, before expansion.

    Mirrors Pi's `InputEvent`. `source` says where the input came from:
    ``"interactive"`` for TUI/print-mode user input, ``"extension"`` for a turn
    an extension started via ``send_user_message``/``send_custom_message``.
    `streaming_behavior` says how the input will be queued when the agent is
    mid-run (``"steer"``/``"follow_up"``), and is ``None`` on the idle prompt
    path.

    Pi's `images` field and ``"rpc"`` source are omitted; Run Agent has neither.
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


@dataclass(frozen=True, slots=True)
class MessageEndHookResult:
    """Replace a finalized message; the replacement must keep the original role."""

    message: AgentMessage | None = None


@dataclass(frozen=True, slots=True)
class ResourcesDiscoverEvent:
    """Fired after `session_start` so extensions can contribute extra resource paths."""

    cwd: str
    reason: Literal["startup", "reload"]
    type: Literal["resources_discover"] = field(default="resources_discover", init=False)


@dataclass(frozen=True, slots=True)
class ResourcesDiscoverResult:
    """Extra skill/prompt/theme directories to merge into this session load."""

    skill_paths: tuple[str, ...] = ()
    prompt_paths: tuple[str, ...] = ()
    theme_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionBeforeSwitchEvent:
    """Fired before `/new` or `/resume` replaces the active session."""

    reason: Literal["new", "resume"]
    target_session_id: str | None = None
    type: Literal["session_before_switch"] = field(default="session_before_switch", init=False)


@dataclass(frozen=True, slots=True)
class SessionBeforeSwitchResult:
    """Cancel a pending session switch."""

    cancel: bool = False


@dataclass(frozen=True, slots=True)
class SessionBeforeForkEvent:
    """Fired before forking a session at an entry."""

    entry_id: str
    position: Literal["before", "at"] = "at"
    type: Literal["session_before_fork"] = field(default="session_before_fork", init=False)


@dataclass(frozen=True, slots=True)
class SessionBeforeForkResult:
    """Cancel a pending session fork."""

    cancel: bool = False


@dataclass(frozen=True, slots=True)
class SessionBeforeCompactEvent:
    """Fired before context compaction; `cancel` skips the compaction."""

    reason: CompactionReason
    will_retry: bool = False
    custom_instructions: str | None = None
    type: Literal["session_before_compact"] = field(default="session_before_compact", init=False)


@dataclass(frozen=True, slots=True)
class SessionBeforeCompactResult:
    """Cancel a pending compaction."""

    cancel: bool = False


@dataclass(frozen=True, slots=True)
class SessionCompactEvent:
    """Fired after context compaction succeeds."""

    reason: CompactionReason
    will_retry: bool = False
    from_extension: bool = False
    type: Literal["session_compact"] = field(default="session_compact", init=False)


@dataclass(frozen=True, slots=True)
class SessionCompactFailedEvent:
    """Fired after context compaction fails or is aborted."""

    reason: CompactionReason
    aborted: bool = False
    will_retry: bool = False
    error_message: str | None = None
    from_extension: bool = False
    type: Literal["session_compact_failed"] = field(default="session_compact_failed", init=False)


@dataclass(frozen=True, slots=True)
class SessionBeforeTreeEvent:
    """Fired before navigating the session tree."""

    target_id: str
    old_leaf_id: str | None = None
    user_wants_summary: bool = False
    type: Literal["session_before_tree"] = field(default="session_before_tree", init=False)


@dataclass(frozen=True, slots=True)
class SessionBeforeTreeResult:
    """Cancel pending session-tree navigation."""

    cancel: bool = False


@dataclass(frozen=True, slots=True)
class SessionTreeEvent:
    """Fired after navigating the session tree."""

    new_leaf_id: str | None = None
    old_leaf_id: str | None = None
    type: Literal["session_tree"] = field(default="session_tree", init=False)


@dataclass(frozen=True, slots=True)
class BeforeProviderRequestEvent:
    """Fired before a provider request is sent; handlers may return a replacement."""

    payload: ModelRequest
    type: Literal["before_provider_request"] = field(default="before_provider_request", init=False)


@dataclass(frozen=True, slots=True)
class BeforeProviderHeadersEvent:
    """Fired after request headers are assembled; handlers mutate `headers` in place."""

    headers: dict[str, str | None]
    type: Literal["before_provider_headers"] = field(default="before_provider_headers", init=False)


@dataclass(frozen=True, slots=True)
class AfterProviderResponseEvent:
    """Fired after a provider HTTP response arrives, before the body is consumed."""

    status: int
    headers: dict[str, str]
    type: Literal["after_provider_response"] = field(default="after_provider_response", init=False)


UIPromptKind = Literal["select", "confirm", "input"]


@dataclass(frozen=True, slots=True)
class UIPromptStartEvent:
    """Fired when the host starts waiting on an extension UI dialog."""

    kind: UIPromptKind
    title: str | None = None
    reason: Literal["ui_prompt"] = "ui_prompt"
    type: Literal["ui_prompt_start"] = field(default="ui_prompt_start", init=False)


@dataclass(frozen=True, slots=True)
class UIPromptEndEvent:
    """Fired when the host is no longer waiting on an extension UI dialog."""

    kind: UIPromptKind
    title: str | None = None
    reason: Literal["ui_prompt"] = "ui_prompt"
    type: Literal["ui_prompt_end"] = field(default="ui_prompt_end", init=False)


@dataclass(frozen=True, slots=True)
class ModelSelectEvent:
    """Fired after the active model changes."""

    model: str
    previous_model: str | None = None
    source: Literal["set", "cycle", "restore"] = "set"
    type: Literal["model_select"] = field(default="model_select", init=False)


@dataclass(frozen=True, slots=True)
class UserBashEvent:
    """Fired before a user-entered bash command runs in the session cwd."""

    command: str
    exclude_from_context: bool
    cwd: str
    type: Literal["user_bash"] = field(default="user_bash", init=False)


@dataclass(frozen=True, slots=True)
class UserBashHookResult:
    """Block or rewrite a user bash command."""

    block: bool = False
    reason: str | None = None
    command: str | None = None


ExtensionHandler = Callable[[object, "ExtensionContext"], object | Awaitable[object]]
# Command handlers are sync-only: the slash-command path (CommandRegistry ->
# CodingSession.handle_command -> TUI submit) is synchronous end to end.
ExtensionCommandHandler = Callable[
    ["str", "ExtensionCommandContext"], "str | None | Awaitable[str | None]"
]


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
        return await self._prompt_ui(
            "select",
            title,
            lambda: self._runtime.ui.select(title, options, timeout=timeout),
        )

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Ask the user to confirm; True only if confirmed."""
        return await self._prompt_ui(
            "confirm",
            title,
            lambda: self._runtime.ui.confirm(title, message, timeout=timeout),
        )

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None:
        """Prompt the user for text; None on cancel/no UI."""
        return await self._prompt_ui(
            "input",
            title,
            lambda: self._runtime.ui.input(title, placeholder, secret=secret, timeout=timeout),
        )

    async def _prompt_ui(
        self,
        kind: UIPromptKind,
        title: str,
        action: Callable[[], Awaitable[_T]],
    ) -> _T:
        self._generation.assert_active()
        await self._runtime.emit_ui_prompt(kind, title)
        try:
            return await action()
        finally:
            await self._runtime.emit_ui_prompt_end(kind, title)

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
    """Session context exposed to extensions.

    Immutable metadata remains readable while a committed runtime is retiring.
    Telemetry, host services, and UI operations always require active mutation
    authority. Once retired, every captured context fails loudly.
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
    def is_active(self) -> bool:
        return self._generation.active

    @property
    def generation_id(self) -> str:
        self._generation.assert_readable()
        return self._generation.id

    @property
    def cwd(self) -> Path:
        self._generation.assert_readable()
        return self._runtime.session_view.cwd

    @property
    def project_resources_enabled(self) -> bool:
        self._generation.assert_readable()
        return self._runtime.session_view.project_resources_enabled

    @property
    def telemetry(self) -> TelemetrySink:
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
        self._generation.assert_readable()
        return self._runtime.paths

    @property
    def environment(self) -> Mapping[str, str]:
        self._generation.assert_readable()
        return self._runtime.environment

    @property
    def model(self) -> str:
        self._generation.assert_readable()
        return self._runtime.session_view.model

    @property
    def provider_name(self) -> str:
        self._generation.assert_readable()
        return self._runtime.session_view.provider_name

    @property
    def session_id(self) -> str | None:
        self._generation.assert_readable()
        return self._runtime.session_view.session_id

    @property
    def current_snapshot_id(self) -> str | None:
        self._generation.assert_readable()
        return self._runtime.session_view.current_snapshot_id

    @property
    def skills(self) -> tuple[Skill, ...]:
        self._generation.assert_readable()
        return tuple(getattr(self._runtime.session_view, "skills", ()))

    @property
    def resource_snapshot(self) -> ExtensionResourceSnapshot:
        self._generation.assert_readable()
        snapshot = self._runtime.context_resources.snapshot
        if snapshot is None:
            raise ExtensionError("Resources are available after Session resource preparation")
        return deepcopy(
            ExtensionResourceSnapshot(
                tuple(item for item in snapshot.providers if item.source_id == self._source_id),
                tuple(
                    item
                    for item in snapshot.contributions
                    if item.provider.source_id == self._source_id
                ),
            )
        )

    @property
    def session_name(self) -> str | None:
        self._generation.assert_readable()
        return self._runtime.session_view.session_name

    @property
    def thinking_level(self) -> str:
        self._generation.assert_readable()
        return self._runtime.session_view.thinking_level

    @property
    def system_prompt(self) -> str:
        self._generation.assert_readable()
        return self._runtime.session_view.system_prompt

    @property
    def is_running(self) -> bool:
        self._generation.assert_readable()
        return self._runtime.session_view.is_running

    @property
    def transcript(self) -> tuple[AgentMessage, ...]:
        self._generation.assert_readable()
        messages = self._runtime.session_view.messages
        return tuple(message.model_copy(deep=True) for message in messages)

    @property
    def has_ui(self) -> bool:
        self._generation.assert_readable()
        return self._runtime.ui.has_ui

    @property
    def ui(self) -> ExtensionUi:
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
        and :class:`MessageRenderOptions` and returns a Rich-markup string, never
        a widget.
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

    async def append_entry(self, namespace: str, data: dict[str, JSONValue]) -> str:
        """Persist extension-owned data to the session as a custom entry."""
        self._generation.assert_active()
        return await self._runtime.append_custom_entry(namespace, data)

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
