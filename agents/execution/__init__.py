"""Provider-neutral local and Docker execution boundary."""

from .base import ExecutionEnvironment
from .backend import SandboxBackend, SandboxSession
from .local import LocalExecutionEnvironment
from .docker import DockerExecutionEnvironment
from .docker_backend import DockerSandboxBackend, DockerSandboxSession
from .errors import SandboxError, SandboxInfrastructureError, SandboxUnavailableError, SandboxWorkspaceError
from .journal import WorkspaceJournal, ChangeSnapshot
from .workspace_prep import prepare_workspace_for_container, scrub_workspace_credentials
from .models import ExecRequest, ExecResult, SandboxSpec
from .workspace import git_base_commit, git_changed_paths, git_diff

__all__ = [
    "ChangeSnapshot",
    "DockerExecutionEnvironment",
    "DockerSandboxBackend",
    "DockerSandboxSession",
    "ExecRequest",
    "ExecResult",
    "ExecutionEnvironment",
    "LocalExecutionEnvironment",
    "SandboxBackend",
    "SandboxError",
    "SandboxInfrastructureError",
    "SandboxSession",
    "SandboxSpec",
    "SandboxUnavailableError",
    "SandboxWorkspaceError",
    "WorkspaceJournal",
    "git_changed_paths",
    "git_diff",
    "git_base_commit",
    "prepare_workspace_for_container",
    "scrub_workspace_credentials",
]
