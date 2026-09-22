"""Durable settings for Run Agent, in the shape of Pi's ``SettingsManager``.

Two JSON files: the user-level ``~/.run/settings.json`` and, when a project has
one, ``<cwd>/.run/settings.json`` deep-merged over it. Keys are camelCase as in
Pi. Two keys are user-level only, because a checked-out project must not be
able to run a shell prefix or relax trust on the person opening it:
``shellCommandPrefix`` and ``defaultProjectTrust``.

Provider, model and thinking level are not settings; they come from the
environment (see ``provider_config``).

``compaction.strategy`` chooses how the Provider view is bounded. It is
project-configurable, because it only picks a preparation policy: ``cheap-first``
(the default) applies the core's free L3/L1/L2 layers and compacts when they are
not enough, ``summary-only`` applies no layer, and ``four-layer`` hands the
decision to a selected extension's ``before_provider_request`` rewrite plus its
``session_compact_request`` commit. The core keeps the hard window guard in all
three cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError, loads
from pathlib import Path
from typing import Any, Literal

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault

QueueMode = Literal["one_at_a_time", "all"]
CompactionStrategy = Literal["cheap-first", "summary-only", "four-layer"]

_USER_ONLY_KEYS = frozenset({"shellCommandPrefix", "shell_command_prefix", "defaultProjectTrust"})


class SettingsError(ValueError):
    """Raised when a Run Agent settings file is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Settings a session starts from, after merging user and project files."""

    shell_command_prefix: str | None = None
    default_project_trust: TrustDefault = "ask"
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"
    compaction_enabled: bool = True
    # `cheap-first` | `summary-only` | `four-layer`; see the module docstring.
    compaction_strategy: CompactionStrategy = "cheap-first"

    def to_json(self) -> dict[str, Any]:
        """Serialize the non-default settings to the on-disk shape."""
        result: dict[str, Any] = {}
        if self.default_project_trust != "ask":
            result["defaultProjectTrust"] = self.default_project_trust
        if self.shell_command_prefix is not None:
            result["shellCommandPrefix"] = self.shell_command_prefix
        if self.steering_mode != "one_at_a_time":
            result["steeringMode"] = _mode_to_json(self.steering_mode)
        if self.follow_up_mode != "one_at_a_time":
            result["followUpMode"] = _mode_to_json(self.follow_up_mode)
        if not self.compaction_enabled or self.compaction_strategy != "cheap-first":
            result["compaction"] = {
                "enabled": self.compaction_enabled,
                "strategy": self.compaction_strategy,
            }
        return result


def settings_path(paths: RunAgentPaths | None = None) -> Path:
    """The user-level settings file."""
    return (paths or RunAgentPaths()).home / "settings.json"


def project_settings_path(cwd: Path, paths: RunAgentPaths | None = None) -> Path:
    """The project-level settings file, next to the project's other ``.run`` inputs."""
    return (paths or RunAgentPaths()).project_run_agent_dir(cwd) / "settings.json"


def load_settings(paths: RunAgentPaths | None = None, cwd: Path | None = None) -> Settings:
    """Load user settings, then let the project's file override what it may."""
    merged = _read_settings_file(settings_path(paths))
    if cwd is not None:
        project = _read_settings_file(project_settings_path(cwd, paths))
        merged = _deep_merge(merged, {k: v for k, v in project.items() if k not in _USER_ONLY_KEYS})
    return settings_from_json(merged)


def settings_from_json(data: dict[str, Any]) -> Settings:
    """Parse settings from JSON-compatible data, reading only known keys."""
    if "shellCommandPrefix" in data and "shell_command_prefix" in data:
        raise SettingsError("Use only one of shellCommandPrefix or shell_command_prefix")

    raw_default = data.get("defaultProjectTrust", "ask")
    if raw_default not in {"ask", "always", "never"}:
        raise SettingsError("defaultProjectTrust must be ask, always, or never")

    raw_prefix = data.get("shellCommandPrefix", data.get("shell_command_prefix"))
    prefix: str | None = None
    if raw_prefix is not None:
        if not isinstance(raw_prefix, str):
            raise SettingsError("shellCommandPrefix must be a string")
        prefix = raw_prefix.strip() or None

    compaction = data.get("compaction", {})
    if not isinstance(compaction, dict):
        raise SettingsError("compaction must be an object")
    enabled = compaction.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError("compaction.enabled must be true or false")
    strategy = compaction.get("strategy", "cheap-first")
    if strategy not in {"cheap-first", "summary-only", "four-layer"}:
        raise SettingsError("compaction.strategy must be cheap-first, summary-only, or four-layer")

    return Settings(
        shell_command_prefix=prefix,
        default_project_trust=raw_default,
        steering_mode=_mode_from_json(data.get("steeringMode"), "steeringMode"),
        follow_up_mode=_mode_from_json(data.get("followUpMode"), "followUpMode"),
        compaction_enabled=enabled,
        compaction_strategy=strategy,
    )


def _mode_from_json(value: object, name: str) -> QueueMode:
    if value is None:
        return "one_at_a_time"
    if value in {"one-at-a-time", "one_at_a_time"}:
        return "one_at_a_time"
    if value == "all":
        return "all"
    raise SettingsError(f"{name} must be one-at-a-time or all")


def _mode_to_json(mode: QueueMode) -> str:
    return "one-at-a-time" if mode == "one_at_a_time" else "all"


def _read_settings_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise SettingsError(f"Settings are not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise SettingsError(f"Settings must be a JSON object: {path}")
    return raw


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
