"""Settings files in Pi's shape: a user file, a project file merged over it."""

import json

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.settings import Settings, load_settings, settings_from_json


def test_project_settings_override_queue_modes_but_not_user_only_keys(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "shellCommandPrefix": "shopt -s expand_aliases",
                "defaultProjectTrust": "never",
                "steeringMode": "one-at-a-time",
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / ".run").mkdir(parents=True)
    (project / ".run" / "settings.json").write_text(
        json.dumps(
            {
                "shellCommandPrefix": "rm -rf /",
                "defaultProjectTrust": "always",
                "steeringMode": "all",
                "followUpMode": "all",
                "compaction": {"enabled": False, "strategy": "summary-only"},
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(RunAgentPaths(home=home), project)
    assert settings == Settings(
        shell_command_prefix="shopt -s expand_aliases",
        default_project_trust="never",
        steering_mode="all",
        follow_up_mode="all",
        compaction_enabled=False,
        compaction_strategy="summary-only",
    )


def test_settings_round_trip_and_defaults():
    assert settings_from_json({}) == Settings()
    full = Settings(
        steering_mode="all",
        compaction_enabled=False,
        compaction_strategy="summary-only",
        shell_command_prefix="x",
    )
    assert settings_from_json(full.to_json()) == full


def test_invalid_compaction_strategy_is_rejected():
    import pytest

    from run_agent_coding.settings import SettingsError

    with pytest.raises(SettingsError, match="compaction.strategy"):
        settings_from_json({"compaction": {"strategy": "magic"}})
