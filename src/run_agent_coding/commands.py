"""Slash command registry for Run Agent coding sessions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from run_agent_coding.prompt_templates import PromptTemplate
from run_agent_coding.reload import CodingReloadSummary, ReloadCategorySummary
from run_agent_coding.resources import ResourceDiagnostic
from run_agent_coding.session_manager import (
    SessionManager,
    normalize_session_name,
)
from run_agent_coding.skills import Skill
from run_agent_coding.system_prompt import ProjectContextFile
from run_agent_coding.thinking import normalize_thinking_level
from run_agent_core.tools import AgentTool


class CommandSession(Protocol):
    """Session attributes available to slash-command handlers."""

    @property
    def cwd(self) -> Path: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def available_models(self) -> Sequence[str]: ...

    @property
    def available_providers(self) -> Sequence[str]: ...

    @property
    def tools(self) -> Sequence[AgentTool]: ...

    @property
    def skills(self) -> Sequence[Skill]: ...

    @property
    def prompt_templates(self) -> Sequence[PromptTemplate]: ...

    @property
    def context_files(self) -> Sequence[ProjectContextFile]: ...

    @property
    def context_token_estimate(self) -> int: ...


    @property
    def context_window_tokens(self) -> int: ...

    @property
    def thinking_level(self) -> str: ...

    @property
    def available_thinking_levels(self) -> Sequence[str]: ...

    @property
    def resource_diagnostics(self) -> Sequence[ResourceDiagnostic]: ...

    @property
    def system_prompt(self) -> str: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def session_title(self) -> str | None: ...

    @property
    def session_manager(self) -> SessionManager | None: ...

    def set_model(self, model: str) -> None: ...

    def reload_provider_settings(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of handling a coding-session slash command."""

    handled: bool
    exit_requested: bool = False
    clear_requested: bool = False
    reload_requested: bool = False
    new_session_requested: bool = False
    export_requested: bool = False
    export_destination: Path | None = None
    export_format: str | None = None
    resume_session_id: str | None = None
    resume_picker_requested: bool = False
    prompts_picker_requested: bool = False
    tree_picker_requested: bool = False
    rewind_entry_id: str | None = None
    fork_entry_id: str | None = None
    model_picker_requested: bool = False
    model_selection_provider: str | None = None
    model_selection_model: str | None = None
    tools_picker_requested: bool = False
    skills_picker_requested: bool = False
    thinking_level: str | None = None
    message: str | None = None
    session_name: str | None = None
    extension_command: str | None = None
    extension_arguments: str = ""


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Runtime context passed to slash-command handlers."""

    session: CommandSession
    registry: CommandRegistry
    text: str
    name: str
    args: str


CommandHandler = Callable[[CommandContext], CommandResult]


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """A registered slash command and its user-facing metadata."""

    name: str
    description: str
    usage: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()


class CommandRegistry:
    """Parse, register, list, and execute slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(self, command: SlashCommand) -> None:
        """Register a slash command and its aliases."""
        name = _normalize_name(command.name)
        if name in self._commands:
            raise ValueError(f"Duplicate slash command: /{name}")
        self._commands[name] = command
        for alias in command.aliases:
            normalized_alias = _normalize_name(alias)
            if normalized_alias in self._commands or normalized_alias in self._aliases:
                raise ValueError(f"Duplicate slash command alias: /{normalized_alias}")
            self._aliases[normalized_alias] = name

    def get(self, name: str) -> SlashCommand | None:
        """Return a command by name or alias."""
        normalized = _normalize_name(name)
        command_name = self._aliases.get(normalized, normalized)
        return self._commands.get(command_name)

    def list_commands(self) -> tuple[SlashCommand, ...]:
        """Return registered commands sorted by name."""
        return tuple(self._commands[name] for name in sorted(self._commands))

    def execute(self, session: CommandSession, text: str) -> CommandResult:
        """Execute a slash command, or return unhandled for ordinary prompts."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return CommandResult(handled=False)

        if stripped.startswith("/skill:"):
            return CommandResult(handled=False)

        name, args = _parse_command(stripped)
        if not name:
            return CommandResult(handled=False)

        command = self.get(name)
        if command is None:
            return CommandResult(handled=False)

        return command.handler(
            CommandContext(session=session, registry=self, text=stripped, name=name, args=args)
        )


def create_default_command_registry() -> CommandRegistry:
    """Create Run Agent's built-in slash command registry."""
    registry = CommandRegistry()
    registry.register(
        SlashCommand(
            name="quit",
            usage="/quit",
            description="Exit the current session.",
            handler=_exit_command,
            aliases=("exit",),
        )
    )
    registry.register(
        SlashCommand(
            name="new",
            usage="/new",
            description="Start a new session.",
            handler=_new_command,
            search_terms=("clear", "reset"),
        )
    )
    registry.register(
        SlashCommand(
            name="export",
            usage="/export [--format html] [destination]",
            description="Export the current session.",
            handler=_export_command,
        )
    )
    registry.register(
        SlashCommand(
            name="session",
            usage="/session",
            description="Show session info and stats.",
            handler=_status_command,
            search_terms=("info",),
        )
    )
    registry.register(
        SlashCommand(
            name="system",
            usage="/system",
            description="Show the active system prompt without saving it.",
            handler=_system_command,
            search_terms=("prompt", "instructions"),
        )
    )
    registry.register(
        SlashCommand(
            name="skill",
            usage="/skill:<name> [request]",
            description="Expand a loaded skill into your prompt.",
            handler=_skill_command,
            search_terms=("skills",),
        )
    )
    registry.register(
        SlashCommand(
            name="skills",
            usage="/skills",
            description="Browse and insert a loaded skill.",
            handler=_skills_command,
            search_terms=("skill", "picker", "search"),
        )
    )
    registry.register(
        SlashCommand(
            name="hotkeys",
            usage="/hotkeys",
            description="Show common keyboard shortcuts.",
            handler=_hotkeys_command,
            search_terms=("keys", "shortcuts", "bindings"),
        )
    )
    registry.register(
        SlashCommand(
            name="prompts",
            usage="/prompts",
            description="Choose a loaded prompt template.",
            handler=_prompts_command,
            search_terms=("templates", "picker"),
        )
    )
    registry.register(
        SlashCommand(
            name="reload",
            usage="/reload",
            description="Reload local resources and project context.",
            handler=_reload_command,
        )
    )
    registry.register(
        SlashCommand(
            name="resume",
            usage="/resume [session-id]",
            description="Resume a previous session.",
            handler=_resume_command,
            search_terms=("history", "previous"),
        )
    )
    registry.register(
        SlashCommand(
            name="tree",
            usage="/tree",
            description="Branch from a previous session entry.",
            handler=_tree_command,
            search_terms=("branch", "history", "fork"),
        )
    )
    registry.register(
        SlashCommand(
            name="rewind",
            usage="/rewind <entry-id>",
            description="Move the current pointer back; abandoned branches stay in the file.",
            handler=_rewind_command,
            search_terms=("back", "pointer", "history"),
        )
    )
    registry.register(
        SlashCommand(
            name="fork",
            usage="/fork <entry-id>",
            description="Copy the path to an entry into a new session and switch to it.",
            handler=_fork_command,
            search_terms=("copy", "branch", "new session"),
        )
    )
    registry.register(
        SlashCommand(
            name="name",
            usage="/name <new name>",
            description="Rename the current session.",
            handler=_name_command,
            search_terms=("rename", "title"),
        )
    )
    registry.register(
        SlashCommand(
            name="model",
            usage="/model",
            description="Choose the active model.",
            handler=_model_command,
        )
    )
    registry.register(
        SlashCommand(
            name="tools",
            usage="/tools",
            description="Browse tools available to the active session.",
            handler=_tools_command,
            search_terms=("capabilities", "reference"),
        )
    )
    registry.register(
        SlashCommand(
            name="help",
            usage="/help",
            description="List available commands.",
            handler=_help_command,
            search_terms=("commands",),
        )
    )
    registry.register(
        SlashCommand(
            name="thinking",
            usage="/thinking [level]",
            description="Show or set the thinking level for future turns.",
            handler=_thinking_command,
            search_terms=("reasoning", "effort"),
        )
    )
    registry.register(
        SlashCommand(
            name="context",
            usage="/context",
            description="Show the active project context files.",
            handler=_context_command,
            search_terms=("agents.md", "instructions"),
        )
    )
    registry.register(
        SlashCommand(
            name="resources",
            usage="/resources",
            description="Summarize loaded skills, prompt templates and context files.",
            handler=_resources_command,
            search_terms=("diagnostics",),
        )
    )
    registry.register(
        SlashCommand(
            name="trace",
            usage="/trace",
            description="Show where this session's spans are recorded, when tracing is on.",
            handler=_trace_command,
            search_terms=("spans", "telemetry", "observability"),
        )
    )
    return registry


def _help_command(context: CommandContext) -> CommandResult:
    lines = ["Available commands:"]
    for command in context.registry.list_commands():
        lines.append(f"{command.usage}\t{command.description}")
    return CommandResult(handled=True, message="\n".join(lines))


def _trace_command(context: CommandContext) -> CommandResult:
    recorder = getattr(context.session, "trace_recorder", None)
    if recorder is None:
        return CommandResult(
            handled=True, message="Tracing is off. Start with --trace to record spans."
        )
    message = (
        f"Trace log: {recorder.path}\nSession: {recorder.session_id}\n"
        f"Recorded spans: {recorder.span_count}; dropped spans: {recorder.dropped_count}"
    )
    return CommandResult(handled=True, message=message)


def _exit_command(context: CommandContext) -> CommandResult:
    return CommandResult(handled=True, exit_requested=True, message="Exiting session.")


def _new_command(context: CommandContext) -> CommandResult:
    return CommandResult(handled=True, new_session_requested=True)




def _export_command(context: CommandContext) -> CommandResult:
    try:
        export_format, destination = _parse_export_args(context.args)
    except ValueError as exc:
        return CommandResult(handled=True, message=str(exc))
    return CommandResult(
        handled=True,
        export_requested=True,
        export_destination=destination,
        export_format=export_format,
    )


def _status_command(context: CommandContext) -> CommandResult:
    session = context.session
    context_usage = getattr(session, "context_usage", None)
    lines = [
        f"Model: {session.model}",
        f"Provider: {session.provider_name}",
        f"CWD: {session.cwd}",
        f"Tools: {len(session.tools)}",
        f"Skills: {len(session.skills)}",
        f"Prompt templates: {len(session.prompt_templates)}",
        f"Context files: {len(session.context_files)}",
        f"Estimated context tokens: {session.context_token_estimate}",
        f"Context window: {session.context_window_tokens}",
    ]
    context_window_source = getattr(session, "context_window_source", None)
    if context_window_source:
        lines.append(f"Context window source: {context_window_source}")
    discovery_error = getattr(session, "model_limits_discovery_error", None)
    if discovery_error:
        lines.append(f"Model limit discovery: unavailable ({discovery_error})")
    if context_usage is not None:
        if context_usage.uses_provider_usage:
            lines.append(
                "Context token basis: "
                f"provider={context_usage.provider_tokens}, "
                f"estimated trailing={context_usage.trailing_tokens}",
            )
        else:
            lines.append(
                "Context token breakdown: "
                f"system={context_usage.system_tokens}, "
                f"messages={context_usage.message_tokens}, "
                f"tools={context_usage.tool_tokens}",
            )
    lines.extend(_thinking_status_lines(session))
    lines.append(f"Resource diagnostics: {len(session.resource_diagnostics)}")
    if session.session_id is not None:
        lines.append(f"Session: {session.session_id}")
    if session.session_title:
        lines.append(f"Session name: {session.session_title}")
    return CommandResult(handled=True, message="\n".join(lines))


def _system_command(context: CommandContext) -> CommandResult:
    if context.args:
        return CommandResult(handled=True, message="Usage: /system")
    return CommandResult(handled=True, message=context.session.system_prompt)


def _hotkeys_command(context: CommandContext) -> CommandResult:
    lines = [
        "Common keyboard shortcuts:",
        "- Enter: submit prompt",
        "- Alt+Enter: insert newline",
        "- Tab: complete a slash command",
        "- Up / Down: input history",
        "- Ctrl+C: stop the current operation or clear input",
        "- /queue <text>: queue a follow-up while running",
        "- /expand <tool-call-id>: show full tool output",
        "- Ctrl+D: quit",
    ]
    return CommandResult(handled=True, message="\n".join(lines))


def _skills_command(context: CommandContext) -> CommandResult:
    if context.args:
        return CommandResult(handled=True, message="Usage: /skills")
    return CommandResult(handled=True, skills_picker_requested=True)


def _resources_command(context: CommandContext) -> CommandResult:
    session = context.session
    lines = [
        f"Skills: {len(session.skills)}",
        f"Prompt templates: {len(session.prompt_templates)}",
        f"Context files: {len(session.context_files)}",
    ]
    if session.resource_diagnostics:
        lines.append("")
        lines.extend(_format_diagnostics(session.resource_diagnostics))
    else:
        lines.append("Resource diagnostics: none")
    return CommandResult(handled=True, message="\n".join(lines))


def _reload_command(context: CommandContext) -> CommandResult:
    # Reload owns async extension lifecycle hooks, so frontends execute it from
    # their async command path rather than inside this synchronous registry.
    return CommandResult(handled=True, reload_requested=True)


def _context_command(context: CommandContext) -> CommandResult:
    session = context.session
    if not session.context_files:
        lines = ["No project context files loaded."]
        if session.resource_diagnostics:
            lines.append("")
            lines.extend(_format_diagnostics(session.resource_diagnostics, kind="context"))
        return CommandResult(handled=True, message="\n".join(lines))

    lines = ["Active project context files:"]
    lines.extend(f"- {context_file.path}" for context_file in session.context_files)
    if session.resource_diagnostics:
        lines.append("")
        lines.extend(_format_diagnostics(session.resource_diagnostics, kind="context"))
    return CommandResult(handled=True, message="\n".join(lines))


def _skill_command(context: CommandContext) -> CommandResult:
    return CommandResult(
        handled=True,
        message="Use /skill:<name> [request] to expand a loaded skill into your prompt.",
    )


def _prompts_command(context: CommandContext) -> CommandResult:
    if context.args:
        return CommandResult(handled=True, message="Usage: /prompts")
    return CommandResult(handled=True, prompts_picker_requested=True)


def _resume_command(context: CommandContext) -> CommandResult:
    if not context.args:
        return CommandResult(handled=True, resume_picker_requested=True)
    manager = context.session.session_manager
    if manager is None:
        return CommandResult(handled=True, message="Session manager is not available.")
    session_id = context.args.strip()
    return CommandResult(
        handled=True,
        resume_session_id=session_id,
    )


def _tree_command(context: CommandContext) -> CommandResult:
    if context.args:
        return CommandResult(handled=True, message="Usage: /tree")
    return CommandResult(handled=True, tree_picker_requested=True)


def _rewind_command(context: CommandContext) -> CommandResult:
    entry_id = context.args.strip()
    if not entry_id:
        return CommandResult(handled=True, message="Usage: /rewind <entry-id>")
    return CommandResult(handled=True, rewind_entry_id=entry_id)


def _fork_command(context: CommandContext) -> CommandResult:
    entry_id = context.args.strip()
    if not entry_id:
        return CommandResult(handled=True, message="Usage: /fork <entry-id>")
    return CommandResult(handled=True, fork_entry_id=entry_id)


def _name_command(context: CommandContext) -> CommandResult:
    manager = context.session.session_manager
    session_id = context.session.session_id
    if manager is None or session_id is None:
        return CommandResult(handled=True, message="Session manager is not available.")

    if not context.args:
        title = context.session.session_title or "Untitled session"
        return CommandResult(
            handled=True,
            message=f"Current session name: {title}\nUsage: /name <new name>",
        )

    try:
        name = _validated_session_name(context.args)
    except ValueError as exc:
        return CommandResult(handled=True, message=str(exc))

    return CommandResult(
        handled=True,
        session_name=name,
        message=f"Session renamed: {name}",
    )


def _tools_command(context: CommandContext) -> CommandResult:
    return CommandResult(handled=True, tools_picker_requested=True)


def _model_command(context: CommandContext) -> CommandResult:
    refresh_error = _refresh_provider_settings(context.session)
    if refresh_error is not None:
        return refresh_error

    if context.args:
        model = context.args.strip()
        available_models = set(context.session.available_models)
        if available_models and model not in available_models:
            models = ", ".join(sorted(available_models))
            return CommandResult(
                handled=True,
                message=f"Unknown model for provider {context.session.provider_name}: {model}\n"
                f"Available models: {models}",
            )
        if callable(getattr(context.session, "select_provider_model", None)):
            return CommandResult(
                handled=True,
                model_selection_provider=context.session.provider_name,
                model_selection_model=model,
            )
        context.session.set_model(model)
        return CommandResult(handled=True, message=f"Current model: {model}")

    return CommandResult(handled=True, model_picker_requested=True)


def _thinking_command(context: CommandContext) -> CommandResult:
    session = context.session
    available = tuple(session.available_thinking_levels)
    if not context.args:
        lines = _thinking_status_lines(session)
        if available:
            lines.append(f"Available modes: {', '.join(available)}")
        else:
            lines.insert(1, f"Current model: {session.provider_name}:{session.model}")
        return CommandResult(handled=True, message="\n".join(lines))

    if not available:
        message = f"Thinking controls are unavailable for {session.provider_name}:{session.model}"
        reason = _thinking_unavailable_reason(session)
        if reason:
            message = f"{message}: {reason}"
        return CommandResult(
            handled=True,
            message=message,
        )
    try:
        level = normalize_thinking_level(context.args)
    except ValueError as exc:
        return CommandResult(handled=True, message=str(exc))
    if level not in available:
        modes = ", ".join(available)
        return CommandResult(
            handled=True,
            message=(
                f"Thinking mode {level} is not available for "
                f"{session.provider_name}:{session.model}\n"
                f"Available modes: {modes}"
            ),
        )
    return CommandResult(handled=True, thinking_level=level)


def _thinking_status_lines(session: CommandSession) -> list[str]:
    if tuple(session.available_thinking_levels):
        return [f"Thinking mode: {session.thinking_level}"]
    lines = ["Thinking mode: unavailable"]
    reason = _thinking_unavailable_reason(session)
    if reason:
        lines.append(f"Thinking unavailable: {reason}")
    return lines


def _thinking_unavailable_reason(session: CommandSession) -> str | None:
    reason = getattr(session, "thinking_unavailable_reason", None)
    return reason if isinstance(reason, str) and reason else None


def _format_diagnostics(
    diagnostics: Sequence[ResourceDiagnostic], *, kind: str | None = None
) -> list[str]:
    filtered = [diagnostic for diagnostic in diagnostics if kind is None or diagnostic.kind == kind]
    if not filtered:
        return ["Resource diagnostics: none"]
    lines = ["Resource diagnostics:"]
    lines.extend(f"- {diagnostic.format()}" for diagnostic in filtered)
    return lines


def _refresh_provider_settings(session: CommandSession) -> CommandResult | None:
    try:
        session.reload_provider_settings()
    except ValueError as exc:
        return CommandResult(
            handled=True,
            message=f"Could not refresh provider settings: {exc}",
        )
    return None


def format_reload_summary(summary: CodingReloadSummary) -> str:
    lines = [
        "Reloaded local coding resources and project context.",
        "Resources:",
        f"- Skills: {_format_reload_category(summary.skills)}",
        f"- Prompt templates: {_format_reload_category(summary.prompt_templates)}",
        f"- Extensions: {_format_reload_category(summary.extensions)}",
        "Context:",
        f"- Project context files: {_format_reload_category(summary.context_files)}",
        "- Next-turn system prompt: "
        + ("rebuilt" if summary.system_prompt_rebuilt else "unchanged"),
        "Diagnostics:",
        f"- Resource diagnostics: {_format_reload_category(summary.diagnostics)}",
        "Provider config:",
        "- Read from the environment at startup; use /model to switch provider or model.",
    ]
    return "\n".join(lines)


def _format_reload_category(summary: ReloadCategorySummary) -> str:
    status = "changed" if summary.changed else "unchanged"
    delta = _format_count_delta(summary.delta)
    suffix = f", {delta}" if delta is not None else ""
    return f"{summary.after} total ({status}{suffix})"


def _format_count_delta(delta: int) -> str | None:
    if delta == 0:
        return None
    return f"{delta:+d}"


def _parse_command(text: str) -> tuple[str, str]:
    command, separator, args = text[1:].partition(" ")
    return _normalize_name(command), args.strip() if separator else ""


def _parse_export_args(args: str) -> tuple[str | None, Path | None]:
    parts = args.split()
    export_format: str | None = None
    destination: Path | None = None
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--format":
            index += 1
            if index >= len(parts):
                raise ValueError("Usage: /export [--format html] [destination]")
            export_format = parts[index]
        elif part.startswith("--format="):
            export_format = part.partition("=")[2]
        elif part.startswith("-"):
            raise ValueError(f"Unknown export option: {part}")
        elif destination is None:
            destination = Path(part).expanduser()
        else:
            raise ValueError("Usage: /export [--format html] [destination]")
        index += 1
    return export_format, destination


def _validated_session_name(value: str) -> str:
    if not value.strip():
        raise ValueError("Usage: /name <new name>")
    return normalize_session_name(value)


def _normalize_name(name: str) -> str:
    return name.strip().removeprefix("/").lower()
