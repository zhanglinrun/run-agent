"""The Core's hard context-budget guard over an immutable session transcript.

The Core measures one Provider request and refuses one that still exceeds the
model window; compaction itself belongs to the ``compaction`` extension, which
rewrites the request in ``before_provider_request`` and commits its result over
``session_compact_request``. Nothing here rewrites anything: an oversized view
raises :class:`ContextBudgetExceeded` before any provider I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from run_agent_coding.context_window import (
    COMPACTION_SUMMARY_PREFIX as REPLAY_SUMMARY_PREFIX,
)
from run_agent_coding.context_window import (
    estimate_context_usage,
)
from run_agent_core.messages import (
    COMPACTION_SUMMARY_PREFIX as PROVIDER_SUMMARY_PREFIX,
)
from run_agent_core.messages import (
    AgentMessage,
    UserMessage,
    message_text,
)
from run_agent_core.provider import ModelRequest

# A compaction replay writes "Previous conversation summary:"; a user message that
# already carries the provider-native summary wrapper is a persisted prefix as well.
# Both are stable while a tail grows, unlike a user request that a later turn replaces.
_SUMMARY_PREFIXES = (REPLAY_SUMMARY_PREFIX, PROVIDER_SUMMARY_PREFIX)


class ContextBudgetExceeded(RuntimeError):
    """The request cannot fit the model window without a persisted summary."""


@dataclass(frozen=True, slots=True)
class ProviderView:
    """The frozen Provider view plus the report of how it was measured.

    ``stable_prefix_digest`` identifies the provider prefix that a caller may
    cache: the system prompt plus the head anchor of the conversation (a
    persisted compaction summary, otherwise the first user request). See
    ``_stable_prefix_digest`` for the exact definition and its invariants.
    """

    request: ModelRequest
    tokens_before: int
    tokens_after: int
    stable_prefix_digest: str


class ContextBudgetGuard:
    """Freeze a Provider view without rewriting it, and refuse an oversized one.

    ``freeze`` only detaches the messages and measures the request; every rewrite
    a session wants has already happened in an extension's
    ``before_provider_request`` handler, and :meth:`require_hard_limit` is the
    non-bypassable last gate before physical I/O.
    """

    def __init__(self, *, context_window_tokens: int) -> None:
        if context_window_tokens < 1:
            raise ValueError("Context window must be positive")
        self.context_window_tokens = context_window_tokens

    def freeze(self, request: ModelRequest) -> ProviderView:
        """Return a detached Provider request and its deterministic measurements."""
        original = tuple(message.model_copy(deep=True) for message in request.messages)
        tokens = self._tokens(request, original)
        return ProviderView(
            request=replace(request, messages=original),
            tokens_before=tokens,
            tokens_after=tokens,
            stable_prefix_digest=_stable_prefix_digest(request.system, original),
        )

    def require_hard_limit(self, view: ProviderView) -> None:
        """Refuse a physical request that still exceeds the model's hard window."""
        if view.tokens_after > self.context_window_tokens:
            raise ContextBudgetExceeded(
                f"context view needs {view.tokens_after} tokens but the model window is "
                f"{self.context_window_tokens}; persistent compaction is required"
            )

    def _tokens(self, request: ModelRequest, messages: tuple[AgentMessage, ...]) -> int:
        return estimate_context_usage(
            system=request.system,
            messages=messages,
            tools=tuple(request.tools),
        ).total_tokens


def _stable_prefix_digest(system: str, messages: tuple[AgentMessage, ...]) -> str:
    """Digest the provider prefix a trailing entry cannot change.

    Definition: SHA-256 over the canonical JSON of the system prompt
    (instructions) plus exactly one head anchor - the persisted compaction
    summary when the head already is one, otherwise the first user request.

    Invariants:

    * Appending to the tail (assistant replies, tool results, further user
      turns) leaves the digest unchanged for as long as no new
      ``CompactionEntry`` is written: neither the system prompt nor the head
      anchor moves.
    * A new compaction rewrites the head with a new summary text, so the digest
      changes. Re-summarizing into byte-identical text provably leaves the
      provider prefix unchanged, so the digest stays equal on purpose: it names
      the prefix, not the number of compaction events.
    """
    kind, anchor = _prefix_anchor(messages)
    encoded = json.dumps([system, kind, anchor], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _prefix_anchor(messages: tuple[AgentMessage, ...]) -> tuple[str, str]:
    """Return the head identity: a compaction summary or the first request."""
    head = messages[0] if messages else None
    if isinstance(head, UserMessage) and head.text.startswith(_SUMMARY_PREFIXES):
        return "summary", head.text
    return "request", next(
        (message_text(message) for message in messages if isinstance(message, UserMessage)),
        "",
    )


__all__ = [
    "ContextBudgetExceeded",
    "ContextBudgetGuard",
    "ProviderView",
]
