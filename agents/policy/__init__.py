"""Runtime constraints for every model-requested action.

The policy package contains hard invariants (plan mode and workspace
boundaries), shell risk classification and scoped confirmation grants.  Tool
adapters may add convenience rules, but they cannot override these invariants.
"""

from .engine import PermissionDecision, PolicyEngine
from .shell import ShellAssessment, ShellRisk, classify_shell_command
from .workspace import WorkspaceBoundary, WorkspaceViolation

__all__ = [
    "PermissionDecision",
    "PolicyEngine",
    "ShellAssessment",
    "ShellRisk",
    "WorkspaceBoundary",
    "WorkspaceViolation",
    "classify_shell_command",
]
