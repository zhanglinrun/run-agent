from pathlib import Path

from run_agent_coding import (
    RunAgentPaths,
    ShellSettings,
    load_shell_settings,
    shell_settings_from_json,
    shell_settings_path,
)


def test_load_shell_settings_missing_file_uses_defaults(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / ".run", agents_home=tmp_path / ".agents")

    assert load_shell_settings(paths) == ShellSettings()


def test_load_shell_settings_accepts_pi_style_shell_command_prefix(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / ".run", agents_home=tmp_path / ".agents")
    path = shell_settings_path(paths)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"shellCommandPrefix": "shopt -s expand_aliases\\nalias gs=\\"git status\\""}',
        encoding="utf-8",
    )

    settings = load_shell_settings(paths)

    assert settings.shell_command_prefix == 'shopt -s expand_aliases\nalias gs="git status"'
    assert settings.to_json() == {
        "shellCommandPrefix": 'shopt -s expand_aliases\nalias gs="git status"'
    }


def test_shell_settings_accepts_run_agent_style_shell_command_prefix() -> None:
    settings = shell_settings_from_json({"shell_command_prefix": " alias ll='ls -la' "})

    assert settings.shell_command_prefix == "alias ll='ls -la'"


def test_shell_settings_ignore_unknown_fields() -> None:
    settings = shell_settings_from_json(
        {
            "shellCommandPrefix": "source ~/.run/aliases.bash",
            "future_shell_option": True,
        }
    )

    assert settings.shell_command_prefix == "source ~/.run/aliases.bash"
