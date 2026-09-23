"""Per-session state, the durable summary record and the view invariants.

The reference implementation keeps its compaction cache in module-level objects
and writes the summary plus ``covered_count``/``retained_tail`` into the session
cache, from which a resumed session rebuilds its view instead of re-summarizing.
This port keeps one :class:`SessionState` per loaded extension, writes a
prepared summary as a ``CustomEntry`` through ``api.append_entry`` (the audit
trail: summary, covered count, retained tail, prompt-side bookkeeping), and
leaves the durable commit itself to the core, which writes the single
``CompactionEntry`` the session replays from. The transcript is never rewritten
by anything here.

Two view invariants live here because every layer needs them:

* :func:`message_key` — the reference identifies messages by ``uuid``; Run
  Agent's messages carry no id, so the extension derives a stable content digest
  and uses it to re-anchor a prepared summary on a later view;
* :func:`legalize_view` — the core's own deterministic ``repair_tool_history``,
  re-applied after every layer, so a rewritten view can never carry an orphan
  tool result or an unresolved tool call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol

from run_agent_coding.context_window import COMPACTION_SUMMARY_PREFIX
from run_agent_core.messages import COMPACTION_SUMMARY_PREFIX as PROVIDER_SUMMARY_PREFIX
from run_agent_core.messages import AgentMessage
from run_agent_core.tool_history import repair_tool_history
from run_agent_core.types import JSONValue

from .config import CompactionConfig

SUMMARY_NAMESPACE = "layered_compaction.summary"
STATUS_NAMESPACE = "layered_compaction.status"


class AppendEntry(Protocol):
    """``api.append_entry``: persist one ``CustomEntry`` and return its id."""

    async def __call__(self, namespace: str, data: dict[str, JSONValue]) -> str: ...


def message_key(message: AgentMessage) -> str:
    """Return the stable identity this extension uses in place of a ``uuid``."""
    payload = json.dumps(
        message.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "lc:" + sha256(payload.encode("utf-8")).hexdigest()[:32]


def legalize_view(messages: Sequence[AgentMessage]) -> tuple[AgentMessage, ...]:
    """Return a provider-legal view: paired tool calls and results, in order."""
    return repair_tool_history(tuple(messages)).messages


@dataclass(frozen=True, slots=True)
class PreparedSummary:
    """A summary produced by L4, waiting to be committed on a request.

    ``covered_count`` is the cut the summary covers (the reference's
    ``covered_count``) and ``retained_tail`` is the snapshot of what was kept
    verbatim, both recorded in the ``CustomEntry`` payload so the durable story
    of one compaction is complete without touching the transcript.
    """

    text: str
    trigger: str
    tokens_before: int
    covered_count: int
    created_at: float
    retained_tail: tuple[dict[str, JSONValue], ...] = ()
    anchor_key: str = ""
    model: str = ""
    snapshot_id: str = ""
    layer: str = "L4"
    custom_instructions: str = ""

    def payload(self) -> dict[str, JSONValue]:
        """Return the ``CustomEntry`` payload for this summary."""
        return {
            "type": "summarized",
            "subtype": "prepared_summary",
            "text": self.text,
            "trigger": self.trigger,
            "tokensBefore": self.tokens_before,
            "coveredCount": self.covered_count,
            "retainedTail": list(self.retained_tail),
            "createdAt": self.created_at,
            "anchorKey": self.anchor_key,
            "model": self.model,
            "snapshotId": self.snapshot_id,
            "layer": self.layer,
            "customInstructions": self.custom_instructions,
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
        raw_count = data.get("coveredCount")
        raw_created = data.get("createdAt")
        raw_tail = data.get("retainedTail")
        raw_model = data.get("model")
        raw_snapshot = data.get("snapshotId")
        raw_layer = data.get("layer")
        raw_instructions = data.get("customInstructions")
        raw_trigger = data.get("trigger")
        tail: tuple[dict[str, JSONValue], ...] = ()
        if isinstance(raw_tail, list):
            tail = tuple(item for item in raw_tail if isinstance(item, dict))
        return cls(
            text=text,
            trigger=str(raw_trigger) if raw_trigger is not None else "auto",
            tokens_before=int(raw_tokens) if isinstance(raw_tokens, (int, float)) else 1,
            covered_count=int(raw_count) if isinstance(raw_count, (int, float)) else 1,
            retained_tail=tail,
            anchor_key=str(data.get("anchorKey") or ""),
            created_at=float(raw_created) if isinstance(raw_created, (int, float)) else 0.0,
            model=str(raw_model) if raw_model is not None else "",
            snapshot_id=str(raw_snapshot) if raw_snapshot is not None else "",
            layer=str(raw_layer) if raw_layer is not None else "L4",
            custom_instructions=str(raw_instructions) if raw_instructions is not None else "",
        )


def active_entry_ids(payload: Mapping[str, JSONValue]) -> tuple[str, ...]:
    """Read the active durable entry ids out of a context-snapshot payload.

    The extension-facing API exposes no entry-id seam, so this is how the commit
    learns a valid ``first_kept_entry_id``: the payload of the last recorded
    agent snapshot carries ``context_entry_ids`` in durable row order.
    """
    raw = payload.get("context_entry_ids")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def resolve_first_kept_entry_id(entry_ids: Sequence[str], covered_count: int) -> str | None:
    """Return the durable boundary entry for a prefix of ``covered_count`` rows.

    ``None`` means the commit must be skipped (fewer than two known rows, or
    nothing to replace); the caller still applies its request-local view. The
    index is clamped below the known tail so a stale id list always errs towards
    keeping more history than the summary covers, never less.
    """
    if covered_count < 1 or len(entry_ids) < 2:
        return None
    index = min(covered_count, len(entry_ids) - 1)
    if index <= 0:
        return None
    return entry_ids[index]


async def persist_summary(append: AppendEntry, summary: PreparedSummary) -> str:
    """Persist one prepared summary as a ``CustomEntry``; return its entry id."""
    return await append(SUMMARY_NAMESPACE, summary.payload())


@dataclass(slots=True)
class SessionState:
    """Mutable per-session state owned by one ``setup(api)`` closure."""

    config: CompactionConfig = field(default_factory=CompactionConfig)
    # Set by ``configure`` at ``session_start``; the extension's own configuration
    # is the only switch.
    rewrite_enabled: bool = False
    prepared: PreparedSummary | None = None
    summary_entry_ids: list[str] = field(default_factory=list)
    entry_ids: tuple[str, ...] = ()
    entry_ids_snapshot: str | None = None
    consecutive_failures: int = 0
    breaker_reason: str | None = None
    reactive_armed: bool = False
    reactive_attempted: bool = False
    commit_emitted: bool = False
    notes: list[str] = field(default_factory=list)
    manual_requested: bool = False
    last_status: int | None = None
    last_request_tokens: int = 0
    last_view: tuple[AgentMessage, ...] = ()

    def breaker_tripped(self) -> bool:
        """Return whether consecutive failures reached the circuit breaker."""
        return self.consecutive_failures >= self.config.max_consecutive_failures

    # -- mutation ------------------------------------------------------------

    def begin_request(self) -> None:
        """Reset per-request latches: one commit per request."""
        self.commit_emitted = False
        self.notes.clear()

    def note(self, text: str) -> None:
        """Record one diagnostic note for the request that is being prepared."""
        self.notes.append(text)

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
        self.prepared = None
        self.summary_entry_ids.clear()
        self.entry_ids = ()
        self.entry_ids_snapshot = None
        self.consecutive_failures = 0
        self.breaker_reason = None
        self.reactive_armed = False
        self.reactive_attempted = False
        self.commit_emitted = False
        self.notes.clear()
        self.manual_requested = False
        self.last_status = None
        self.last_request_tokens = 0
        self.last_view = ()


def summary_message_text(summary: str) -> str:
    """Return the view text of a compaction summary head.

    The one form run-agent already uses for a replayed ``CompactionEntry``
    (``run_agent_coding.context_window.COMPACTION_SUMMARY_PREFIX``, mirrored by
    the session memory replay), so the request that commits a summary and every
    request after it present the provider the *same* head message.
    """
    return f"{COMPACTION_SUMMARY_PREFIX}{summary}"


def view_summary_text(text: str) -> str:
    """Return the summary inside a summary head, or ``''`` when it is not one."""
    for prefix in (COMPACTION_SUMMARY_PREFIX, PROVIDER_SUMMARY_PREFIX):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return ""


__all__ = [
    "STATUS_NAMESPACE",
    "SUMMARY_NAMESPACE",
    "AppendEntry",
    "PreparedSummary",
    "SessionState",
    "active_entry_ids",
    "legalize_view",
    "message_key",
    "persist_summary",
    "resolve_first_kept_entry_id",
    "summary_message_text",
    "view_summary_text",
]
