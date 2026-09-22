"""Standalone memory extension ported from hermes-agent.

Self-contained package: a provider contract, a fan-out manager with a serialized
commit queue, the built-in ``MEMORY.md`` / ``USER.md`` file provider with hermes'
frozen-snapshot semantics, and the ``setup(api)`` wiring that registers the
``memory`` tool and the ``/memory`` command.

Only stable names are re-exported here. The package is deliberately absent from
``run_agent_extensions.BUILTIN_EXTENSIONS``: it loads explicitly through
``--extension <this directory>`` until the migration that removes memory from the
``experience`` extension is complete.
"""

from __future__ import annotations

from .extension import (
    MEMORY_SECTION_HEADER,
    MEMORY_TOOL_DESCRIPTION,
    MEMORY_USAGE,
    PROMPT_GUIDELINE,
    HermesMemoryConfig,
    MemoryCall,
    MemoryOperation,
    inject_recall_block,
    load_hermes_memory_config,
    memory_result,
    refused,
    resolve_stores,
    setup,
)
from .manager import (
    DEFAULT_EXTERNAL_PREFETCH_TIMEOUT_S,
    DEFAULT_SYNC_DRAIN_TIMEOUT_S,
    RESERVED_TOOL_NAMES,
    DrainStatus,
    FlushResult,
    MemoryManager,
)
from .provider import (
    INDICATOR_GLYPH,
    TRIVIAL_PROMPT_RE,
    MemoryProvider,
    RecallStatus,
    StreamingContextScrubber,
    build_memory_context_block,
    is_trivial_prompt,
    memory_provider_tools_enabled,
    normalize_tool_schema,
    sanitize_context,
)
from .store import (
    BLOCK_HEADERS,
    DEFAULT_LIMITS,
    ENTRY_DELIMITER,
    MEMORY_FILES,
    SNAPSHOT_PREAMBLE,
    BuiltinMemoryProvider,
    MemoryCallOutcome,
    MemoryFile,
    MemoryScope,
    MemoryStore,
    MemoryTarget,
    MemoryWrite,
    approve_memory_write,
    first_threat_refusal,
    require_memory_mutation,
    run_memory_call,
    scan_entry_for_threats,
)

__all__ = [
    "BLOCK_HEADERS",
    "DEFAULT_EXTERNAL_PREFETCH_TIMEOUT_S",
    "DEFAULT_LIMITS",
    "DEFAULT_SYNC_DRAIN_TIMEOUT_S",
    "ENTRY_DELIMITER",
    "INDICATOR_GLYPH",
    "MEMORY_FILES",
    "MEMORY_SECTION_HEADER",
    "MEMORY_TOOL_DESCRIPTION",
    "MEMORY_USAGE",
    "PROMPT_GUIDELINE",
    "RESERVED_TOOL_NAMES",
    "SNAPSHOT_PREAMBLE",
    "TRIVIAL_PROMPT_RE",
    "BuiltinMemoryProvider",
    "DrainStatus",
    "FlushResult",
    "HermesMemoryConfig",
    "MemoryCall",
    "MemoryCallOutcome",
    "MemoryFile",
    "MemoryManager",
    "MemoryOperation",
    "MemoryProvider",
    "MemoryScope",
    "MemoryStore",
    "MemoryTarget",
    "MemoryWrite",
    "RecallStatus",
    "StreamingContextScrubber",
    "approve_memory_write",
    "build_memory_context_block",
    "first_threat_refusal",
    "inject_recall_block",
    "is_trivial_prompt",
    "load_hermes_memory_config",
    "memory_provider_tools_enabled",
    "memory_result",
    "normalize_tool_schema",
    "refused",
    "require_memory_mutation",
    "resolve_stores",
    "run_memory_call",
    "sanitize_context",
    "scan_entry_for_threats",
    "setup",
]
