"""Errors raised by the task sandbox layer."""


class SandboxError(RuntimeError):
    """Base class for sandbox failures."""


class SandboxUnavailableError(SandboxError):
    """The selected backend cannot be used on this host."""


class SandboxInfrastructureError(SandboxError):
    """A container failed, timed out, or could not be cleaned up."""


class SandboxWorkspaceError(SandboxError):
    """A workspace or command violated the sandbox contract."""


__all__ = ["SandboxError", "SandboxInfrastructureError", "SandboxUnavailableError", "SandboxWorkspaceError"]
