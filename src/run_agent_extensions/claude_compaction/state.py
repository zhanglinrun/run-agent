"""Per-session four-layer state, durable markers and view invariants.

The reference implementation keeps its state in module-level objects
(``cachedMCState``, ``lastSummarizedMessageId``) and stores snip boundaries as
``system`` messages inside the transcript. This port keeps one
:class:`SessionState` per loaded extension and writes snip boundaries and
prepared summaries as ``CustomEntry`` records through
``api.append_entry``/``history.read_custom``; the session transcript itself is
never rewritten.

Three view invariants live here because every layer needs them:

* :func:`message_key` — the reference identifies messages by ``uuid``; Run
  Agent's messages carry no id, so the extension derives a stable content digest
  and uses it as the ``removedUuids`` analogue.
* :func:`protected_prefix_length` — the cacheable prefix (the leading summary
  head or first user message) is never trimmed by L1/L2.
* :func:`legalize_view` — the port's ``ensureToolResultPairing``: the core's own
  deterministic repair, re-applied after every layer so a rewritten view can
  never carry an orphan tool result or an unresolved tool call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from time import time
from typing import Protocol

from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    CompactionSummaryMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.session.entries import CustomEntry
from run_agent_core.tool_history import repair_tool_history
from run_agent_core.types import JSONValue

from .config import FourLayerConfig

SNIP_NAMESPACE = "claude_compaction.snip"
SUMMARY_NAMESPACE = "claude_compaction.summary"
STATUS_NAMESPACE = "claude_compaction.status"

# The reference's snip boundary text (`snipCompact.ts`/`force-snip.ts`).
SNIP_BOUNDARY_TEXT = "[snip] Conversation history before this point has been snipped."


class AppendEntry(Protocol):
    """``api.append_entry``: persist one ``CustomEntry`` and return its id."""

    async def __call__(self, namespace: str, data: dict[str, JSONValue]) -> str: ...


class ReadCustomEntry(Protocol):
    """``services.history.read_custom``: read one persisted ``CustomEntry``."""

    async def __call__(self, entry_id: str) -> CustomEntry: ...


def message_key(message: AgentMessage) -> str:
    """Return the stable identity this extension uses in place of a ``uuid``."""
    payload = json.dumps(
        message.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "cc:" + sha256(payload.encode("utf-8")).hexdigest()[:32]


def protected_prefix_length(messages: Sequence[AgentMessage]) -> int:
    """Return how many leading messages every layer must keep.

    A leading summary head (``CompactionSummaryMessage``) or the first user
    message is the stable prefix a provider cache is keyed on, so L1 never
    clears a tool result inside it and L2 never removes it.
    """
    if not messages:
        return 0
    head = messages[0]
    if isinstance(head, (CompactionSummaryMessage, UserMessage)):
        return 1
    return 0


def legalize_view(messages: Sequence[AgentMessage]) -> tuple[AgentMessage, ...]:
    """Return a provider-legal view: paired tool calls and results, in order."""
    return repair_tool_history(tuple(messages)).messages


def message_text_length(message: AgentMessage) -> int:
    """Return the serialized character count of one message (snip accounting)."""
    return len(json.dumps(message.model_dump(mode="json"), ensure_ascii=False))


@dataclass(frozen=True, slots=True)
class SnipBoundary:
    """One durable ``snip_boundary`` marker: what it removed and why."""

    removed: tuple[str, ...]
    created_at: float
    trigger: str
    reason: str | None = None
    tokens_freed: int = 0
    text: str = SNIP_BOUNDARY_TEXT

    def payload(self) -> dict[str, JSONValue]:
        """Return the ``CustomEntry`` payload for this boundary."""
        return {
            "type": "system",
            "subtype": "snip_boundary",
            "content": self.text,
            "isMeta": True,
            "snipMetadata": {
                "removedUuids": list(self.removed),
                "trigger": self.trigger,
                "reason": self.reason,
                "tokensFreed": self.tokens_freed,
                "createdAt": self.created_at,
            },
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, JSONValue]) -> SnipBoundary | None:
        """Rebuild a boundary from a stored payload, or ``None`` when malformed."""
        if data.get("subtype") != "snip_boundary":
            return None
        metadata = data.get("snipMetadata")
        if not isinstance(metadata, Mapping):
            return None
        raw_removed = metadata.get("removedUuids")
        if not isinstance(raw_removed, list):
            return None
        removed = tuple(item for item in raw_removed if isinstance(item, str))
        reason = metadata.get("reason")
        text = data.get("content")
        raw_tokens = metadata.get("tokensFreed")
        raw_created = metadata.get("createdAt")
        raw_trigger = metadata.get("trigger")
        return cls(
            removed=removed,
            created_at=float(raw_created) if isinstance(raw_created, (int, float)) else 0.0,
            trigger=str(raw_trigger) if raw_trigger is not None else "unknown",
            reason=reason if isinstance(reason, str) else None,
            tokens_freed=int(raw_tokens) if isinstance(raw_tokens, (int, float)) else 0,
            text=text if isinstance(text, str) and text else SNIP_BOUNDARY_TEXT,
        )


@dataclass(frozen=True, slots=True)
class PreparedSummary:
    """A summary produced by L3/L4, waiting to be committed on a request."""

    text: str
    trigger: str
    tokens_before: int
    replaced_rows: int
    created_at: float
    anchor_key: str = ""
    model: str = ""
    snapshot_id: str = ""
    layer: str = "L4"

    def payload(self) -> dict[str, JSONValue]:
        """Return the ``CustomEntry`` payload for this summary."""
        return {
            "type": "summarized",
            "subtype": "prepared_summary",
            "text": self.text,
            "trigger": self.trigger,
            "tokensBefore": self.tokens_before,
            "replacedRows": self.replaced_rows,
            "createdAt": self.created_at,
            "anchorKey": self.anchor_key,
            "model": self.model,
            "snapshotId": self.snapshot_id,
            "layer": self.layer,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, JSONValue]) -> PreparedSummary | None:
        """Rebuild a prepared summary from a stored payload, or ``None``."""
        if data.get("subtype") != "prepared_summary":
            return None
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        raw_tokens = data.get("tokensBefore")
        raw_rows = data.get("replacedRows")
        raw_created = data.get("createdAt")
        raw_trigger = data.get("trigger")
        raw_model = data.get("model")
        raw_snapshot = data.get("snapshotId")
        raw_layer = data.get("layer")
        return cls(
            text=text,
            trigger=str(raw_trigger) if raw_trigger is not None else "auto",
            tokens_before=int(raw_tokens) if isinstance(raw_tokens, (int, float)) else 1,
            replaced_rows=int(raw_rows) if isinstance(raw_rows, (int, float)) else 1,
            anchor_key=str(data.get("anchorKey") or ""),
            created_at=float(raw_created) if isinstance(raw_created, (int, float)) else 0.0,
            model=str(raw_model) if raw_model is not None else "",
            snapshot_id=str(raw_snapshot) if raw_snapshot is not None else "",
            layer=str(raw_layer) if raw_layer is not None else "L4",
        )


def active_entry_ids(payload: Mapping[str, JSONValue]) -> tuple[str, ...]:
    """Read the active durable entry ids out of a context-snapshot payload.

    The extension-facing API exposes no entry-id seam, so this is how L3/L4 learn
    a valid ``first_kept_entry_id``: the payload of the last recorded agent
    snapshot carries ``context_entry_ids`` in durable row order.
    """
    raw = payload.get("context_entry_ids")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def resolve_first_kept_entry_id(entry_ids: Sequence[str], replaced_rows: int) -> str | None:
    """Return the durable boundary entry for a prefix of ``replaced_rows`` rows.

    ``None`` means the commit must be skipped (fewer than two known rows, or
    nothing to replace); the caller still applies its request-local view. The
    index is clamped below the known tail so a stale id list always errs towards
    keeping more history than the summary covers, never less.
    """
    if replaced_rows < 1 or len(entry_ids) < 2:
        return None
    index = min(replaced_rows, len(entry_ids) - 1)
    if index <= 0:
        return None
    return entry_ids[index]


async def persist_boundary(append: AppendEntry, boundary: SnipBoundary) -> str:
    """Persist one snip boundary as a ``CustomEntry``; return its entry id."""
    return await append(SNIP_NAMESPACE, boundary.payload())


async def read_boundary(reader: ReadCustomEntry, entry_id: str) -> SnipBoundary | None:
    """Read back a persisted snip boundary."""
    entry = await reader(entry_id)
    return SnipBoundary.from_payload(entry.data)


async def persist_summary(append: AppendEntry, summary: PreparedSummary) -> str:
    """Persist one prepared summary as a ``CustomEntry``; return its entry id."""
    return await append(SUMMARY_NAMESPACE, summary.payload())


async def read_summary(reader: ReadCustomEntry, entry_id: str) -> PreparedSummary | None:
    """Read back a persisted prepared summary."""
    entry = await reader(entry_id)
    return PreparedSummary.from_payload(entry.data)


def new_boundary(
    removed: Sequence[str],
    *,
    trigger: str,
    reason: str | None = None,
    tokens_freed: int = 0,
) -> SnipBoundary:
    """Build a boundary stamped with the current time."""
    return SnipBoundary(
        removed=tuple(removed),
        created_at=time(),
        trigger=trigger,
        reason=reason,
        tokens_freed=tokens_freed,
    )


@dataclass(slots=True)
class SessionState:
    """Mutable per-session state owned by one ``setup(api)`` closure."""

    config: FourLayerConfig = field(default_factory=FourLayerConfig)
    rewrite_enabled: bool = False
    strategy_seen: str = "unknown"
    boundaries: list[SnipBoundary] = field(default_factory=list)
    boundary_entry_ids: list[str] = field(default_factory=list)
    prepared: PreparedSummary | None = None
    summary_entry_ids: list[str] = field(default_factory=list)
    last_summarized_key: str | None = None
    entry_ids: tuple[str, ...] = ()
    entry_ids_snapshot: str | None = None
    consecutive_failures: int = 0
    breaker_reason: str | None = None
    reactive_armed: bool = False
    reactive_attempted: bool = False
    commit_emitted: bool = False
    notes: list[str] = field(default_factory=list)
    nudged_count: int = 0
    manual_requested: bool = False
    last_status: int | None = None
    last_request_tokens: int = 0
    view_message_count: int = 0
    last_view: tuple[AgentMessage, ...] = ()

    # -- derived views -------------------------------------------------------

    @property
    def removed_keys(self) -> frozenset[str]:
        """Return every message key removed by any recorded boundary."""
        removed: set[str] = set()
        for boundary in self.boundaries:
            removed.update(boundary.removed)
        return frozenset(removed)

    @property
    def tokens_freed(self) -> int:
        """Return the total ``tokensFreed`` reported by recorded boundaries."""
        return sum(boundary.tokens_freed for boundary in self.boundaries)

    def breaker_tripped(self) -> bool:
        """Return whether consecutive failures reached the circuit breaker."""
        return self.consecutive_failures >= self.config.max_consecutive_failures

    # -- mutation ------------------------------------------------------------

    def begin_request(self) -> None:
        """Reset per-request latches: one commit per request, no repeated nudges."""
        self.commit_emitted = False
        self.notes.clear()

    def note(self, text: str) -> None:
        """Record one diagnostic note for the request that is being prepared."""
        self.notes.append(text)

    def record_boundary(self, boundary: SnipBoundary, entry_id: str | None) -> None:
        """Remember one snip boundary and the ``CustomEntry`` that stores it."""
        self.boundaries.append(boundary)
        if entry_id is not None:
            self.boundary_entry_ids.append(entry_id)

    def record_prepared(self, summary: PreparedSummary, entry_id: str | None) -> None:
        """Remember a prepared summary waiting for the next request."""
        self.prepared = summary
        if entry_id is not None:
            self.summary_entry_ids.append(entry_id)

    def consume_prepared(self) -> PreparedSummary | None:
        """Take the prepared summary, if any, and clear it."""
        summary = self.prepared
        self.prepared = None
        return summary

    def record_entry_ids(self, entry_ids: Sequence[str], snapshot_id: str | None) -> None:
        """Remember the freshest known active durable entry ids."""
        if entry_ids:
            self.entry_ids = tuple(entry_ids)
            self.entry_ids_snapshot = snapshot_id

    def note_failure(self, reason: str) -> int:
        """Count one consecutive L4 failure; return the new count."""
        self.consecutive_failures += 1
        if self.breaker_tripped():
            self.breaker_reason = reason
        return self.consecutive_failures

    def note_success(self) -> None:
        """Reset the failure counter after a successful commit or summary."""
        self.consecutive_failures = 0
        self.breaker_reason = None

    def reset_run(self) -> None:
        """Start-of-run reset: the reactive path gets one attempt per run."""
        self.reactive_attempted = False
        self.commit_emitted = False
        self.notes.clear()

    def reset_session(self) -> None:
        """Start-of-session reset for every mutable field."""
        self.boundaries.clear()
        self.boundary_entry_ids.clear()
        self.prepared = None
        self.summary_entry_ids.clear()
        self.last_summarized_key = None
        self.entry_ids = ()
        self.entry_ids_snapshot = None
        self.consecutive_failures = 0
        self.breaker_reason = None
        self.reactive_armed = False
        self.reactive_attempted = False
        self.commit_emitted = False
        self.notes.clear()
        self.nudged_count = 0
        self.manual_requested = False
        self.last_status = None
        self.last_request_tokens = 0
        self.view_message_count = 0
        self.last_view = ()


def tool_call_ids(message: AgentMessage) -> tuple[ToolCall, ...]:
    """Return the tool calls of an assistant message (empty for other roles)."""
    return message.tool_calls if isinstance(message, AssistantMessage) else ()


def result_call_id(message: AgentMessage) -> str | None:
    """Return the tool-call id a tool-result message answers."""
    return message.tool_call_id if isinstance(message, ToolResultMessage) else None


__all__ = [
    "SNIP_BOUNDARY_TEXT",
    "SNIP_NAMESPACE",
    "STATUS_NAMESPACE",
    "SUMMARY_NAMESPACE",
    "AppendEntry",
    "PreparedSummary",
    "ReadCustomEntry",
    "SessionState",
    "SnipBoundary",
    "active_entry_ids",
    "legalize_view",
    "message_key",
    "message_text_length",
    "new_boundary",
    "persist_boundary",
    "persist_summary",
    "protected_prefix_length",
    "read_boundary",
    "read_summary",
    "resolve_first_kept_entry_id",
    "result_call_id",
    "tool_call_ids",
]
