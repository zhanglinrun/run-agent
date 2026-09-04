from pathlib import Path

from run_agent_coding.context import discover_project_context
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.resources import RunAgentResourcePaths


def test_discovers_user_project_and_agents_context_files(tmp_path: Path) -> None:
    run_agent_home = tmp_path / "home" / ".run"
    agents_home = tmp_path / "home" / ".agents"
    project = tmp_path / "project"
    nested = project / "pkg"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (run_agent_home).mkdir(parents=True)
    (agents_home).mkdir(parents=True)
    (project / ".run").mkdir()
    (project / ".agents").mkdir()

    (run_agent_home / "AGENTS.md").write_text("User Run Agent instructions", encoding="utf-8")
    (agents_home / "AGENTS.md").write_text("User agents instructions", encoding="utf-8")
    (project / "AGENTS.md").write_text("Project instructions", encoding="utf-8")
    (nested / "AGENTS.md").write_text("Nested instructions", encoding="utf-8")
    (nested / ".run").mkdir()
    (nested / ".agents").mkdir()
    (nested / ".run" / "AGENTS.md").write_text("Project Run Agent instructions", encoding="utf-8")
    (nested / ".agents" / "AGENTS.md").write_text("Project agents instructions", encoding="utf-8")

    context_files = discover_project_context(
        RunAgentResourcePaths(
            root=run_agent_home,
            agents_root=agents_home,
            cwd=nested,
            paths=RunAgentPaths(home=run_agent_home, agents_home=agents_home),
        )
    )

    assert [Path(context_file.path) for context_file in context_files] == [
        run_agent_home / "AGENTS.md",
        agents_home / "AGENTS.md",
        project / "AGENTS.md",
        nested / "AGENTS.md",
        nested / ".run" / "AGENTS.md",
        nested / ".agents" / "AGENTS.md",
    ]
    assert [context_file.content for context_file in context_files] == [
        "User Run Agent instructions",
        "User agents instructions",
        "Project instructions",
        "Nested instructions",
        "Project Run Agent instructions",
        "Project agents instructions",
    ]
