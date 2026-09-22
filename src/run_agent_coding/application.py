"""Shared application lifecycle for terminal, print and evaluation hosts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from run_agent_coding.commands import CommandResult, format_reload_summary
from run_agent_coding.events import CodingSessionEvent
from run_agent_coding.extensions.api import StderrUiBridge, UiBridge
from run_agent_coding.host.inputs import InputSource
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault, TrustOverride
from run_agent_coding.provider_config import ProviderSettings, load_provider_settings
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_coding.session import CodingSession, CodingSessionConfig, ModelChoice
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.settings import load_settings
from run_agent_coding.thinking import ThinkingLevel
from run_agent_core.provider import ModelProvider
from run_agent_core.session import SessionState, load_session_entries, resolve_active_leaf_id
from run_agent_extensions import BUILTIN_EXTENSIONS


@dataclass(frozen=True, slots=True)
class ApplicationOptions:
    cwd: Path
    paths: RunAgentPaths | None = None
    provider_name: str | None = None
    model: str | None = None
    resume: str | None = None
    session_id: str | None = None
    extension_paths: tuple[Path, ...] = ()
    extensions_enabled: bool = True
    project_extensions_enabled: bool = False
    trace_enabled: bool = False
    trust_override: TrustOverride | None = None
    trust_default: TrustDefault | None = None
    thinking: ThinkingLevel | None = None
    system: str | None = None
    pinned_resources: bool = False
    refresh_resources: bool = False


class CodingApplication:
    def __init__(
        self, session: CodingSession, manager: SessionManager, *, owns_manager: bool
    ) -> None:
        self.session = session
        self.manager = manager
        self._owns_manager = owns_manager
        self._started = False

    @classmethod
    async def open(
        cls,
        options: ApplicationOptions,
        *,
        manager: SessionManager | None = None,
        provider: ModelProvider | None = None,
        settings: ProviderSettings | None = None,
        committer: object | None = None,
        provider_transform: Callable[[ModelProvider, str], ModelProvider] | None = None,
        input_source: InputSource | None = None,
    ) -> CodingApplication:
        paths = options.paths or (manager.paths if manager is not None else RunAgentPaths())
        owns_manager = manager is None
        manager = manager or SessionManager(paths)
        try:
            if options.refresh_resources and options.pinned_resources:
                raise ValueError("Pinned background resources cannot be refreshed")
            settings = settings or (load_provider_settings(paths) if provider is None else None)
            shell = load_settings(paths, options.cwd)
            if options.resume is not None:
                record = await manager.get_session(options.resume)
                if record is None:
                    raise ValueError(f"Unknown session: {options.resume}")
            else:
                record = await manager.create_session(
                    cwd=options.cwd,
                    model=options.model or "",
                    provider_name=options.provider_name,
                    session_id=options.session_id,
                )
            storage = await manager.open_storage(record.id, committer=committer)
            extension_paths = options.extension_paths
            if options.extensions_enabled:
                defaults = tuple(BUILTIN_EXTENSIONS.values())
                if options.resume is not None and not options.refresh_resources:
                    entries = await load_session_entries(storage)
                    head = await storage.get_head()
                    leaf_id = resolve_active_leaf_id(entries) or head.entry_id
                    state = SessionState.from_entries(entries, leaf_id=leaf_id)
                    snapshot = next(
                        (
                            entry
                            for entry in reversed(state.custom_entries)
                            if entry.namespace == "run.resources"
                        ),
                        None,
                    )
                    if snapshot is not None:
                        payload = snapshot.data.get("payload")
                        sources = payload.get("extensions", []) if isinstance(payload, dict) else []
                        source_ids = (
                            {
                                source.get("source_id")
                                for source in sources
                                if isinstance(source, dict)
                            }
                            if isinstance(sources, list)
                            else set()
                        )
                        defaults = tuple(
                            path
                            for path in defaults
                            if f"extension:{(path / 'extension.py').as_uri()}" in source_ids
                        )
                extension_paths = tuple(dict.fromkeys((*extension_paths, *defaults)))
            session = await CodingSession.load(
                CodingSessionConfig(
                    provider=provider,
                    provider_transform=provider_transform,
                    input_source=input_source,
                    pinned_resources=options.pinned_resources,
                    refresh_resources=options.refresh_resources,
                    model=options.model or record.model,
                    storage=storage,
                    telemetry=await manager.telemetry(),
                    host_services=await manager.host_services(),
                    skill_packages=await manager.skill_packages(),
                    cwd=record.cwd,
                    session_id=record.id,
                    session_manager=manager,
                    system=options.system,
                    resource_paths=RunAgentResourcePaths(
                        root=paths.home, cwd=record.cwd, agents_root=paths.agents_home, paths=paths
                    ),
                    provider_name=options.provider_name
                    or record.provider_name
                    or (settings.default_provider if settings else "test"),
                    requested_provider=options.provider_name,
                    requested_model=options.model,
                    session_provider_name=record.provider_name,
                    provider_settings=settings,
                    thinking_level_override=options.thinking,
                    extension_paths=extension_paths,
                    extensions_enabled=options.extensions_enabled,
                    project_extensions_enabled=options.project_extensions_enabled,
                    trace_enabled=options.trace_enabled,
                    trust_override=options.trust_override,
                    trust_default=options.trust_default or shell.default_project_trust,
                    shell_command_prefix=shell.shell_command_prefix,
                    steering_mode=shell.steering_mode,
                    follow_up_mode=shell.follow_up_mode,
                    auto_compact_enabled=shell.compaction_enabled,
                    compaction_strategy=shell.compaction_strategy,
                )
            )
            return cls(session, manager, owns_manager=owns_manager)
        except BaseException:
            if owns_manager:
                await manager.aclose()
            raise

    async def start(self, ui: UiBridge | None = None) -> None:
        if self._started:
            return
        self.session.extension_runtime.set_ui_bridge(ui or StderrUiBridge())
        await self.session.emit_pending_session_start()
        self._started = True

    async def prompt(
        self, text: str, *, run_id: str | None = None
    ) -> AsyncIterator[CodingSessionEvent]:
        if not self._started:
            await self.start()
        events = self.session.prompt(text, run_id=run_id)
        try:
            async for event in events:
                yield event
        finally:
            closer = getattr(events, "aclose", None)
            if closer is not None:
                await closer()

    async def command(self, text: str) -> CommandResult:
        """Execute each parsed intent exactly once, on the async application path."""
        if not self._started:
            await self.start()
        session = self.session
        if session.is_running:
            if text.strip() == "/stop":
                session.cancel()
                return CommandResult(handled=True, message="Stopping current run…")
            if text.startswith("/queue "):
                session.queue_follow_up_message(text[7:].strip())
                return CommandResult(handled=True, message="Queued for the next turn.")
            if text.strip() not in {"/session", "/help", "/hotkeys"}:
                return CommandResult(
                    handled=True, message="Stop the current run before changing the session."
                )
        if text.startswith("/branch "):
            branch = await session.branch_to_entry(text[8:].strip())
            return CommandResult(handled=True, message=branch.message)
        result = session.handle_command(text)
        ui = session.extension_runtime.ui
        message = result.message
        if result.extension_command is not None:
            message = await session.extension_runtime.execute_command(
                result.extension_command,
                result.extension_arguments,
            )
        elif result.new_session_requested:
            message = await session.new_session()
        elif result.resume_session_id is not None:
            message = await session.resume(result.resume_session_id)
        elif result.resume_picker_requested:
            records = await self.manager.list_sessions(session.cwd)
            labels = [f"{item.id}  {item.title or 'Untitled'}" for item in records]
            selected = await ui.select("Resume session", labels) if ui.has_ui and labels else None
            message = (
                await session.resume(records[labels.index(selected)].id)
                if selected
                else "\n".join(labels) or "No sessions found."
            )
        elif result.tree_picker_requested:
            choices = await session.tree_choices()
            labels = [f"{item.entry_id}  {item.label}" for item in choices]
            selected = (
                await ui.select("Branch from entry", labels) if ui.has_ui and labels else None
            )
            message = (
                (await session.branch_to_entry(choices[labels.index(selected)].entry_id)).message
                if selected
                else "\n".join(labels) or "No entries yet."
            )
        elif result.rewind_entry_id is not None:
            message = await session.rewind(result.rewind_entry_id)
        elif result.fork_entry_id is not None:
            message = await session.fork_session(result.fork_entry_id)
        elif result.session_name is not None:
            message = f"Session renamed: {await session.set_session_name(result.session_name)}"
        elif result.reload_requested:
            message = format_reload_summary(await session.reload())
        elif result.compact_summary is not None:
            message = await session.compact(result.compact_summary or None)
        elif result.export_requested:
            message = str(
                await session.export(result.export_destination, format=result.export_format)
            )
        elif result.thinking_level is not None:
            message = await session.set_thinking_level(result.thinking_level)
        elif result.model_selection_model is not None:
            changed = await session.select_provider_model(
                ModelChoice(
                    result.model_selection_provider or session.provider_name,
                    result.model_selection_model,
                )
            )
            message = f"Model: {changed.choice.provider_name}:{changed.choice.model}"
        elif result.model_picker_requested:
            choices_model = session.available_model_choices
            model_labels = [f"{item.provider_name}:{item.model}" for item in choices_model]
            selected = await ui.select("Model", model_labels) if ui.has_ui else None
            if selected:
                await session.select_provider_model(choices_model[model_labels.index(selected)])
            message = f"Model: {selected}" if selected else "\n".join(model_labels)
        elif result.tools_picker_requested:
            message = "\n".join(f"{tool.name}: {tool.description}" for tool in session.tools)
        elif result.skills_picker_requested:
            message = (
                "\n".join(f"/skill:{skill.name}: {skill.description}" for skill in session.skills)
                or "No skills loaded."
            )
        elif result.prompts_picker_requested:
            message = (
                "\n".join(f"/{item.name}: {item.description}" for item in session.prompt_templates)
                or "No prompt templates."
            )
        return replace(result, message=message)

    async def aclose(self) -> None:
        try:
            await self.session.aclose()
        finally:
            if self._owns_manager:
                await self.manager.aclose()

    async def __aenter__(self) -> CodingApplication:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
