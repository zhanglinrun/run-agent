"""Settings files in Pi's shape: a user file, a project file merged over it.

Compaction is not a setting any more: the built-in `compaction` extension owns it.
A legacy `compaction` key (or its `enabled`/`strategy` members) must be ignored
rather than rejected, so an existing `settings.json` keeps loading.
"""

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
    )


def test_settings_round_trip_and_defaults():
    assert settings_from_json({}) == Settings()
    full = Settings(
        steering_mode="all",
        shell_command_prefix="x",
    )
    assert settings_from_json(full.to_json()) == full


def test_a_legacy_compaction_key_is_ignored():
    """A retired key never fails a load, whatever it holds."""
    assert settings_from_json({"compaction": {"enabled": False}}) == Settings()
    assert settings_from_json({"compaction": {"strategy": "magic"}}) == Settings()
    assert settings_from_json({"compaction": {"enabled": True, "strategy": "four-layer"}}) == (
        Settings()
    )
    assert settings_from_json({"compaction": "four-layer"}) == Settings()
    assert "compaction" not in Settings().to_json()
