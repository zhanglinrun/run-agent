"""Public Python extension API and default capability profile."""

from .contracts import (
    CommandRegistration,
    EXTENSION_API_VERSION,
    ExtensionAPI,
    ExtensionContext,
    ExtensionEvent,
    ExtensionSpec,
    PromptContribution,
    PromptRenderContext,
    RunOutcome,
    SourceInfo,
    ToolHandlerResult,
    ToolRegistration,
)
from .defaults import DEFAULT_EXTENSION_NAMES, default_extension_specs
from .host import ExtensionHost, ExtensionLoadError, ExtensionToolExecutor
from .loader import ExtensionDiscoveryError, discover_extension_specs, load_extension_spec

__all__ = [
    "CommandRegistration",
    "DEFAULT_EXTENSION_NAMES",
    "EXTENSION_API_VERSION",
    "ExtensionAPI",
    "ExtensionContext",
    "ExtensionDiscoveryError",
    "ExtensionEvent",
    "ExtensionHost",
    "ExtensionLoadError",
    "ExtensionSpec",
    "ExtensionToolExecutor",
    "PromptContribution",
    "PromptRenderContext",
    "RunOutcome",
    "SourceInfo",
    "ToolHandlerResult",
    "ToolRegistration",
    "default_extension_specs",
    "discover_extension_specs",
    "load_extension_spec",
]
