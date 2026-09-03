"""Conservative shell-command risk classification.

This is deliberately an allowlist for read-only and verification commands.
Unknown commands require confirmation instead of being assumed harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class ShellRisk(str, Enum):
    READ_ONLY = "read_only"
    VERIFY = "verify"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ShellAssessment:
    risk: ShellRisk
    reason: str


_DESTRUCTIVE = (
    re.compile(r"\brm\b", re.IGNORECASE),
    re.compile(r"\bdel\b", re.IGNORECASE),
    re.compile(r"\brmdir\b", re.IGNORECASE),
    re.compile(r"\bremove-item\b", re.IGNORECASE),
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\b", re.IGNORECASE),
    re.compile(r"\b(shutdown|reboot|kill|pkill|taskkill|stop-process)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(reset|clean|checkout|restore)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\b.*(?:--force|-f)\b", re.IGNORECASE),
)
_EXTERNAL = (
    re.compile(r"\b(git\s+(push|pull|fetch)|curl|wget|invoke-webrequest|ssh|scp)\b", re.IGNORECASE),
    re.compile(r"\b(pip|pip3|uv)\s+install\b", re.IGNORECASE),
    re.compile(r"\b(npm|pnpm|yarn)\s+(install|add|publish)\b", re.IGNORECASE),
    re.compile(r"\b(docker\s+(push|login)|kubectl\s+(apply|delete))\b", re.IGNORECASE),
)
_WORKSPACE_WRITE = (
    re.compile(r"(?:^|[;&|])\s*(set-content|add-content|out-file|new-item|move-item|copy-item)\b", re.IGNORECASE),
    re.compile(r"(?:^|[;&|])\s*(touch|mkdir|mv|cp)\b", re.IGNORECASE),
    re.compile(r"(^|[^>])>{1,2}(?!>)"),
    re.compile(r"\bgit\s+(add|commit|merge|rebase|cherry-pick)\b", re.IGNORECASE),
)
_VERIFY = (
    re.compile(r"^(?:python(?:\.exe)?\s+-m\s+)?(?:pytest|compileall|py_compile)\b", re.IGNORECASE),
    re.compile(r"^(?:npm|pnpm|yarn)\s+(?:test|run\s+(?:test|lint|typecheck|build))\b", re.IGNORECASE),
    re.compile(r"^(?:go\s+test|cargo\s+(?:test|check)|mvn\s+test|gradle\s+test|\.\\gradlew\s+test)\b", re.IGNORECASE),
)
_READ_ONLY = (
    re.compile(r"^(?:pwd|echo|whoami|where|which|ls|dir|tree)\b", re.IGNORECASE),
    re.compile(r"^(?:get-childitem|get-content|select-string|test-path)\b", re.IGNORECASE),
    re.compile(r"^(?:rg|grep|findstr)\b", re.IGNORECASE),
    re.compile(r"^git\s+(?:status|diff|log|show|branch|rev-parse|ls-files)\b", re.IGNORECASE),
    re.compile(r"^(?:python(?:\.exe)?\s+--version|node\s+--version|npm\s+--version)\b", re.IGNORECASE),
)
_SHELL_CONTROL = re.compile(r"(?:\r|\n|;|&&|\|\||\||`|\$\(|[<>])")


def classify_shell_command(command: str) -> ShellAssessment:
    text = str(command or "").strip()
    if not text:
        return ShellAssessment(ShellRisk.UNKNOWN, "empty shell command")
    for pattern in _DESTRUCTIVE:
        if pattern.search(text):
            return ShellAssessment(ShellRisk.DESTRUCTIVE, f"destructive pattern: {pattern.pattern}")
    for pattern in _EXTERNAL:
        if pattern.search(text):
            return ShellAssessment(ShellRisk.EXTERNAL, f"external side effect: {pattern.pattern}")
    for pattern in _WORKSPACE_WRITE:
        if pattern.search(text):
            return ShellAssessment(ShellRisk.WORKSPACE_WRITE, f"shell file/state mutation: {pattern.pattern}")
    # Safe-command recognition is intentionally limited to one simple
    # command.  Otherwise a harmless prefix such as `git status` could hide a
    # second command with side effects that is not covered by the patterns
    # above.  Compound commands and shell control syntax require confirmation.
    if _SHELL_CONTROL.search(text):
        return ShellAssessment(ShellRisk.UNKNOWN, "compound command or shell control syntax requires confirmation")
    for pattern in _VERIFY:
        if pattern.search(text):
            return ShellAssessment(ShellRisk.VERIFY, "recognized verification command")
    for pattern in _READ_ONLY:
        if pattern.search(text):
            return ShellAssessment(ShellRisk.READ_ONLY, "recognized read-only command")
    return ShellAssessment(ShellRisk.UNKNOWN, "command is not on the read-only allowlist")
