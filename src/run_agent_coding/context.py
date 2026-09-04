"""Project instruction discovery for Run Agent coding sessions."""

from __future__ import annotations

from pathlib import Path

from run_agent_coding.resources import ResourceDiagnostic, RunAgentResourcePaths
from run_agent_coding.system_prompt import ProjectContextFile

PROJECT_MARKERS = (".git", "pyproject.toml", "uv.lock", "setup.py", "package.json")


def discover_project_context(
    paths: RunAgentResourcePaths | None = None,
) -> tuple[ProjectContextFile, ...]:
    """Discover project instruction files for system prompt context."""
    context_files, _diagnostics = discover_project_context_with_diagnostics(paths)
    return context_files


def discover_project_context_with_diagnostics(
    paths: RunAgentResourcePaths | None = None,
) -> tuple[tuple[ProjectContextFile, ...], tuple[ResourceDiagnostic, ...]]:
    """Discover project instruction files and return non-fatal diagnostics."""
    resource_paths = paths or RunAgentResourcePaths()
    context_files: list[ProjectContextFile] = []
    diagnostics: list[ResourceDiagnostic] = []
    for path in _context_file_candidates(resource_paths):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="context",
                    path=path,
                    message=f"could not read context file: {exc}",
                )
            )
            continue
        context_files.append(ProjectContextFile(path=str(path), content=content))
    return tuple(context_files), tuple(diagnostics)


def _context_file_candidates(paths: RunAgentResourcePaths) -> tuple[Path, ...]:
    candidates: list[Path] = [paths.root / "AGENTS.md"]
    if paths.agents_root is not None:
        candidates.append(paths.agents_root / "AGENTS.md")

    if paths.cwd is not None and paths.project_resources_enabled:
        cwd = paths.cwd.expanduser().resolve()
        project_root = _find_project_root(cwd)
        candidates.extend(_ancestor_agents_files(project_root, cwd))
        run_agent_paths = paths._paths()
        candidates.extend(
            [
                run_agent_paths.project_run_agent_dir(cwd) / "AGENTS.md",
                run_agent_paths.project_agents_dir(cwd) / "AGENTS.md",
            ]
        )

    existing = [path for path in candidates if path.is_file()]
    return tuple(_dedupe_resolved_paths(existing))


def _find_project_root(cwd: Path) -> Path:
    for path in (cwd, *cwd.parents):
        if any((path / marker).exists() for marker in PROJECT_MARKERS):
            return path
    return cwd


def _ancestor_agents_files(project_root: Path, cwd: Path) -> list[Path]:
    try:
        relative = cwd.relative_to(project_root)
    except ValueError:
        return [cwd / "AGENTS.md"]

    paths = [project_root / "AGENTS.md"]
    current = project_root
    for part in relative.parts:
        current = current / part
        paths.append(current / "AGENTS.md")
    return paths


def _dedupe_resolved_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped
