"""Canonical filesystem paths for Run Agent user and project data."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunAgentPaths:
    """Resolved Run Agent filesystem locations.

    Run Agent keeps durable application data under the user's home directory while also
    loading project-local resources from the active working directory.
    """

    home: Path = field(default_factory=lambda: Path.home() / ".run")
    agents_home: Path = field(default_factory=lambda: Path.home() / ".agents")

    @property
    def database_path(self) -> Path:
        return self.home / "state.sqlite3"

    @property
    def logs_dir(self) -> Path:
        """Return Run Agent's user-level diagnostic log directory."""
        return self.home / "logs"

    @property
    def extension_state_dir(self) -> Path:
        """Return the user-level state directory owned by extensions."""
        return self.home / "state" / "extensions"

    @property
    def user_skills_dir(self) -> Path:
        """Return Run Agent's user-level skills directory."""
        return self.home / "skills"

    @property
    def user_prompts_dir(self) -> Path:
        """Return Run Agent's user-level prompt templates directory."""
        return self.home / "prompts"

    @property
    def user_themes_dir(self) -> Path:
        """Return Run Agent's user-level TUI themes directory."""
        return self.home / "themes"

    @property
    def user_extensions_dir(self) -> Path:
        """Return Run Agent's user-level extension directory."""
        return self.home / "extensions"

    @property
    def user_agents_skills_dir(self) -> Path:
        """Return the user-level `.agents/skills` directory."""
        return self.agents_home / "skills"

    @property
    def user_agents_prompts_dir(self) -> Path:
        """Return the user-level `.agents/prompts` directory."""
        return self.agents_home / "prompts"

    def project_run_agent_dir(self, cwd: Path) -> Path:
        """Return the project-local Run Agent resource directory."""
        return cwd / ".run"

    def project_agents_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents` resource directory."""
        return cwd / ".agents"

    def project_skills_dir(self, cwd: Path) -> Path:
        """Return the project-local Run Agent skills directory."""
        return self.project_run_agent_dir(cwd) / "skills"

    def project_prompts_dir(self, cwd: Path) -> Path:
        """Return the project-local Run Agent prompt templates directory."""
        return self.project_run_agent_dir(cwd) / "prompts"

    def project_themes_dir(self, cwd: Path) -> Path:
        """Return the project-local Run Agent TUI themes directory."""
        return self.project_run_agent_dir(cwd) / "themes"

    def project_agents_skills_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents/skills` directory."""
        return self.project_agents_dir(cwd) / "skills"

    def project_agents_prompts_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents/prompts` directory."""
        return self.project_agents_dir(cwd) / "prompts"
