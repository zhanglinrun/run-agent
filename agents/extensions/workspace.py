"""Execution and workspace-tool extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from ..execution import DockerExecutionEnvironment, ExecRequest, LocalExecutionEnvironment, SandboxSpec
from .contracts import ExtensionAPI, ExtensionContext, ToolHandlerResult


def _definition(name: str) -> dict[str, Any]:
    from ..tools.registry import tool_definitions

    for item in tool_definitions:
        if item.get("name") == name:
            return {key: value for key, value in item.items() if key != "deferred"}
    raise KeyError(name)


def _handler_result(content: str) -> ToolHandlerResult:
    if content.startswith("Error"):
        return ToolHandlerResult(content, ok=False, error=content)
    return ToolHandlerResult(content)


async def _create_execution(task: Any, workspace: Path) -> Any:
    settings = task.runtime.execution
    if settings.environment is not None:
        return settings.environment
    if settings.backend == "local":
        return LocalExecutionEnvironment(workspace)
    if settings.backend != "docker":
        raise ValueError(f"unsupported execution backend: {settings.backend}")
    spec = settings.sandbox_spec or SandboxSpec(
        workspace=workspace,
        image=settings.sandbox_image,
        network=settings.network,
        memory_mb=settings.memory_mb,
        cpus=settings.cpus,
        pids_limit=settings.pids_limit,
        timeout_seconds=settings.timeout_seconds,
        patch_timeout_seconds=settings.patch_timeout_seconds,
        container_workspace=settings.container_workspace,
        python_executable=settings.python_executable,
        verification_profile=task.verification_profile,
        run_id=task.runtime.session.run_id or "",
        case_id=task.task_id,
    )
    if Path(spec.workspace).expanduser().resolve() != workspace:
        raise ValueError("SandboxSpec workspace must match TaskSpec.workspace")
    return await DockerExecutionEnvironment(
        spec,
        backend=settings.sandbox_backend,
        session=settings.sandbox_session,
    ).start()


def setup_execution(api: ExtensionAPI) -> None:
    """Register the sole execution-environment factory."""

    api.register_execution_factory(_create_execution)


@dataclass
class WorkspaceToolService:
    execution: Any
    read_file_state: dict[str, int] = field(default_factory=dict)

    def _host_path(self, raw: str) -> Path:
        root = Path(self.execution.workspace_root).resolve()
        path = Path(raw).expanduser()
        candidate = (path if path.is_absolute() else root / path).resolve()
        candidate.relative_to(root)
        return candidate

    async def read_file(self, value: dict[str, Any]) -> str:
        raw = str(value["file_path"])
        content = await self.execution.read_file(raw)
        if not content.startswith("Error"):
            path = self._host_path(raw)
            try:
                self.read_file_state[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
        return content

    def _fresh_read_error(self, raw: str, verb: str) -> str | None:
        path = self._host_path(raw)
        if not path.exists():
            return None
        seen = self.read_file_state.get(str(path))
        if seen is None:
            return f"Error: read {raw} before {verb} it."
        if path.stat().st_mtime_ns != seen:
            return f"Error: {raw} changed since it was read; read it again before {verb} it."
        return None

    async def write_file(self, value: dict[str, Any]) -> str:
        raw = str(value["file_path"])
        denied = self._fresh_read_error(raw, "writing")
        if denied:
            return denied
        result = await self.execution.write_file(raw, str(value["content"]))
        if not result.startswith("Error"):
            path = self._host_path(raw)
            self.read_file_state[str(path)] = path.stat().st_mtime_ns
        return result

    async def edit_file(self, value: dict[str, Any]) -> str:
        raw = str(value["file_path"])
        denied = self._fresh_read_error(raw, "editing")
        if denied:
            return denied
        result = await self.execution.edit_file(
            raw,
            str(value["old_string"]),
            str(value["new_string"]),
        )
        if not result.startswith("Error"):
            path = self._host_path(raw)
            self.read_file_state[str(path)] = path.stat().st_mtime_ns
        return result

    async def run_shell(self, value: dict[str, Any]) -> ToolHandlerResult:
        command = str(value["command"])
        result = await self.execution.exec(
            ExecRequest(
                self.execution.shell_argv(command),
                cwd="/workspace",
                timeout_seconds=max(0.001, float(value.get("timeout", 120000)) / 1000.0),
            )
        )
        if result.ok:
            return ToolHandlerResult(result.stdout or "(no output)")
        detail = result.stderr or result.stdout or "command failed"
        return ToolHandlerResult(
            f"Command failed (exit code {result.exit_code})\n{detail}",
            ok=False,
            error=detail,
        )


def _workspace_tools(context: ExtensionContext) -> WorkspaceToolService:
    service = context.services.get("workspace_tools")
    if service is None or service.execution is not context.execution:
        service = WorkspaceToolService(context.execution)
        context.services["workspace_tools"] = service
    return service


def setup_workspace_tools(api: ExtensionAPI) -> None:
    async def session_start(_event: Any, context: ExtensionContext) -> None:
        settings = context.task.runtime.execution
        if settings.backend == "local" and not settings.allow_host_shell:
            active = set(api.get_active_tools())
            active.discard("run_shell")
            api.set_active_tools(active)

    async def read_file(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        return _handler_result(await _workspace_tools(context).read_file(value))

    async def write_file(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        return _handler_result(await _workspace_tools(context).write_file(value))

    async def edit_file(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        return _handler_result(await _workspace_tools(context).edit_file(value))

    async def list_files(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        content = await context.execution.list_files(
            str(value["pattern"]), str(value.get("path") or ".")
        )
        return _handler_result(content)

    async def grep_search(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        content = await context.execution.search(
            str(value["pattern"]),
            str(value.get("path") or "."),
            value.get("include"),
        )
        return _handler_result(content)

    async def run_shell(value: dict[str, Any], context: ExtensionContext) -> ToolHandlerResult:
        return await _workspace_tools(context).run_shell(value)

    async def tool_search(value: dict[str, Any], context: ExtensionContext) -> str:
        matches = context.host.search_tools(
            str(value.get("query") or ""), context
        )
        if not matches:
            return "No matching deferred tools found."
        return json.dumps(matches, ensure_ascii=False, indent=2)

    api.register_tool(
        _definition("read_file"),
        read_file,
        prompt_snippet="`read_file` reads a workspace file with line numbers.",
        prompt_guidelines=("Use `read_file` before changing an existing file.",),
    )
    api.register_tool(
        _definition("write_file"),
        write_file,
        prompt_snippet="`write_file` creates or replaces a workspace file.",
        prompt_guidelines=("Use `write_file` only for complete file content inside the workspace.",),
    )
    api.register_tool(
        _definition("edit_file"),
        edit_file,
        prompt_snippet="`edit_file` performs one exact, unique replacement.",
        prompt_guidelines=("Use `edit_file` for narrow changes after reading the current file.",),
    )
    api.register_tool(
        _definition("list_files"),
        list_files,
        prompt_snippet="`list_files` finds workspace files by glob.",
    )
    api.register_tool(
        _definition("grep_search"),
        grep_search,
        prompt_snippet="`grep_search` searches workspace file contents.",
    )
    api.register_tool(
        _definition("run_shell"),
        run_shell,
        prompt_snippet="`run_shell` runs a system command in the selected execution environment.",
        prompt_guidelines=(
            "Use `run_shell` for terminal operations only when a typed workspace tool is not suitable.",
        ),
    )
    api.register_tool(
        _definition("tool_search"),
        tool_search,
        prompt_snippet="`tool_search` activates deferred tools by name or description.",
    )
    api.on("session_start", session_start)


__all__ = ["setup_execution", "setup_workspace_tools"]
