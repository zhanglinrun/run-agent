"""Memory-provider contract, deterministic recall indicators, context fencing.

Ported from hermes-agent (``agent/memory_provider.py`` plus the fencing helpers of
``agent/memory_manager.py``). The contract is kept verbatim where Run Agent has an
equivalent concept; the two adaptations are documented in the package README:

- conversation history is Run Agent's ``AgentMessage`` sequence, never the
  OpenAI-style ``list[dict]`` hermes passes around;
- ``memory_provider_tools_enabled`` drops hermes' toolset-alias resolution
  because Run Agent has no toolsets registry.

Memory providers give the agent persistent recall across sessions. The
``MemoryManager`` enforces a one-external-provider limit to prevent tool schema
bloat and conflicting memory backends.

Lifecycle (called by ``MemoryManager``):
  initialize()           — connect, create resources, warm up
  system_prompt_block()  — static text for the system prompt
  prefetch(query)        — recall before each turn
  sync_turn(user, asst)  — background write after each turn
  get_tool_schemas()     — tool schemas to expose to the model
  handle_tool_call()     — dispatch a tool call
  shutdown()             — clean exit

Optional hooks (override to opt in):
  on_turn_start(turn_number, message, **kwargs)  — per-turn tick with context
  on_session_end(messages)                       — end-of-session extraction
  on_session_switch(new_session_id, **kwargs)     — mid-process session rotation
  on_pre_compress(messages) -> str               — extract before compression
  on_memory_write(action, target, content, metadata) — mirror built-in writes
  on_delegation(task, result, **kwargs)          — parent-side subagent observation
  backup_paths() -> list[str]                    — extra paths for backups
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from run_agent_core.messages import AgentMessage

logger = logging.getLogger(__name__)

# Default glyph for the deterministic memory indicators. Providers override
# per-status with their own brand mark (hermes' Hindsight uses "👁️").
INDICATOR_GLYPH = "🧠"


@dataclass(frozen=True, slots=True)
class RecallStatus:
    """Summary of what a provider's most recent prefetch injected this turn.

    Returned by :meth:`MemoryProvider.recall_status` so the agent can emit a
    deterministic, model-independent "memory was used" indicator (see
    ``MemoryManager.describe_recall``). ``count`` is the number of discrete
    memories injected; ``0`` means content was injected but has no discrete
    count (e.g. a synthesized reflect answer), which the indicator renders
    generically rather than as "0 memories". ``glyph`` is the brand mark the
    indicator leads with.
    """

    provider_label: str
    count: int
    glyph: str = INDICATOR_GLYPH


# Prompts that carry no semantic signal — trivial acknowledgements, greetings,
# slash commands, empty input. Single source of truth shared by the per-turn
# prefetch gate and provider-side classifiers so the two can never drift apart.
# The alternation is anchored and may only be followed by whitespace or
# punctuation, so words that merely START with a trivial word ("k8s", "yolo",
# "note") do NOT match, while trailing-punctuation variants ("hi!", "thanks :)",
# "done???") do.
TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r"[\s!?.:;,\"'~‘’“”—–…()\[\]{}<>*&^%$#@!+=`\u00a0]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """Return True if a user prompt is too trivial to warrant memory recall.

    Empty/whitespace-only input, slash commands, and bare greetings or
    acknowledgements (with optional trailing punctuation) all count as trivial.
    Callers use this to skip memory-provider prefetch/injection on turns that
    carry no semantic signal — saving a blocking round-trip and preventing stale
    user-model context from derailing one-word replies.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("/"):
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'honcho')."""

    # -- Core lifecycle (implement these) -------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured, has credentials and is ready.

        Called during agent init to decide whether to activate the provider.
        Must not make network calls — only check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs: object) -> None:
        """Initialize for a session.

        Called once at agent startup. May create resources, establish
        connections or start background threads.

        kwargs always include ``home`` (the active Run Agent home directory, for
        profile-scoped storage instead of a hardcoded ``~/.run``) and may include
        ``agent_context`` (``"primary"``/``"subagent"``/``"evaluation"``),
        ``parent_session_id`` and ``platform``. Providers must skip writes for
        non-primary contexts: a system prompt from an evaluation or maintenance
        context would corrupt a user representation.
        """

    @abstractmethod
    def get_tool_schemas(self) -> list[Mapping[str, object]]:
        """Return the tool schemas this provider exposes.

        Each schema is a bare function schema
        (``{"name": ..., "description": ..., "parameters": {...}}``); callers wrap
        it as ``{"type": "function", "function": schema}``. Return an empty list
        for a context-only provider.
        """

    def unavailable_reason(self) -> str:
        """Actionable reason this provider reports unavailable, for the caller.

        ``is_available()`` gates initialization, so a provider that reports
        unavailable is never initialized and any diagnostic it would log from
        ``initialize()`` is unreachable. Return a short, user-facing hint here
        (e.g. which package to install). Empty string (the default) adds nothing.
        """
        return ""

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip. This
        is for STATIC provider info (instructions, status). Prefetched recall
        context is injected separately via ``prefetch()``.
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each request. Return formatted text to inject as context,
        or empty string when nothing relevant. Implementations should be fast —
        use background threads for the actual recall and return cached results.
        ``session_id`` is provided for providers serving concurrent sessions
        (cached agents); providers without per-session scoping can ignore it.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed by
        ``prefetch()`` on the next turn. Default is a no-op — providers that do
        background prefetching should override this.
        """
        return None

    def recall_status(self) -> RecallStatus | None:
        """Describe what the most recent :meth:`prefetch` injected, for the UI.

        Called right after prefetch so the caller can surface a deterministic
        "🧠 recalled N memories" status line that does not depend on the model
        choosing to mention it.

        Return ``None`` (the default) when this provider injected nothing this
        turn or does not want a visible indicator. Providers that override it
        must reflect only the LAST prefetch — never a stale prior count.
        """
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Sequence[AgentMessage] | None = None,
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Should be non-blocking — queue for background
        processing if the backend has latency.

        ``messages`` is the conversation as of the completed turn, including
        assistant tool calls and tool results. Providers that do not need raw
        turn context can ignore it.
        """
        return None

    def handle_tool_call(self, tool_name: str, args: Mapping[str, object], **kwargs: object) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result). Only called for tool names
        returned by ``get_tool_schemas()``.
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""
        return None

    # -- Optional hooks (override to opt in) ---------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs: object) -> None:
        """Called at the start of each turn with the user message.

        Use for turn counting, scope management, periodic maintenance. kwargs may
        include ``remaining_tokens``, ``model`` and ``tool_count``; providers use
        what they need and ignore the rest.
        """
        return None

    def on_session_end(self, messages: Sequence[AgentMessage]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Use for end-of-session fact extraction or summarization. ``messages`` is
        the full conversation history.

        NOT called after every turn — only at actual session boundaries (process
        exit, ``/reset``, ``/new``).
        """
        return None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: object,
    ) -> None:
        """Called when the agent switches session_id mid-process.

        Fires on ``/resume``, ``/branch``, ``/reset``, ``/new`` and context
        compression — any path that reassigns the session identity without
        tearing the provider down.

        Providers that cached per-session state in ``initialize()``
        (``_session_id``, accumulated turn buffers, counters) must update or reset
        that state here so subsequent writes land in the correct session record.

        Parameters
        ----------
        new_session_id:
            The session_id the agent just switched to.
        parent_session_id:
            The previous session_id, when meaningful — set for ``/branch`` (fork
            lineage), compression (continuation lineage) and ``/resume`` (the
            session being left). Empty string when no lineage applies.
        reset:
            ``True`` when this is a genuinely new conversation rather than a
            resumption of an existing one. Providers should flush accumulated
            per-session buffers when this is set. ``False`` for ``/resume``,
            ``/branch`` and compression, where the logical conversation continues
            under the new id.
        rewound:
            ``True`` if the session identity is unchanged but the transcript was
            truncated; providers caching per-turn document state should
            invalidate.

        Default is a no-op.
        """
        return None

    def on_pre_compress(self, messages: Sequence[AgentMessage]) -> str:
        """Called before context compression discards old messages.

        Use to extract insights from the messages about to be compressed. Return
        text to carry into the compression summary so the compressor preserves
        provider-extracted insights. Return empty string for no contribution.
        """
        return ""

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs: object
    ) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's provider gets the task/result pair as an observation of what
        was delegated and what came back. The subagent itself has no provider
        session.

        task: the delegation prompt; result: the subagent's final response;
        child_session_id: the subagent's session_id.
        """
        return None

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Called when the built-in memory surface writes an entry.

        action: 'add', 'replace' or 'remove'; target: 'memory' or 'user';
        content: the entry content.

        ``metadata`` is structured provenance for the write, when available.
        Common keys: ``write_origin``, ``execution_context``, ``session_id``,
        ``parent_session_id``, ``platform``, ``tool_name`` and ``old_text`` for
        replace/remove. Use it to mirror built-in memory writes into a backend
        while keeping the origin of each fact auditable.
        """
        return None

    def get_config_schema(self) -> list[Mapping[str, object]]:
        """Return the config fields this provider needs for setup.

        Each field is a mapping with ``key`` and ``description``, optionally
        ``secret``, ``required``, ``default``, ``choices``, ``type``, ``minimum``,
        ``maximum``, ``step``, ``url`` or ``env_var``. Return an empty list when no
        configuration is needed (local-only providers).
        """
        return []

    def save_config(self, values: Mapping[str, object], home: str) -> None:
        """Write non-secret config to the provider's native location.

        Called after collecting user inputs. ``values`` holds only non-secret
        fields (secrets belong in the environment) and ``home`` is the active Run
        Agent home directory. Providers that use only environment variables can
        leave the default (no-op).
        """
        return None

    def backup_paths(self) -> list[str]:
        """Return extra on-disk paths this provider stores outside the home directory.

        Backup tooling only walks the Run Agent home directory, so any provider
        state kept under ``~/.honcho``, ``~/.hindsight``, etc. is lost across a
        backup/import cycle unless declared here.

        Return absolute path strings (files or directories). Must be callable
        without ``initialize()`` and without network — resolve from config/env
        only. Default returns an empty list (nothing external).
        """
        return []


def normalize_tool_schema(schema: object) -> dict[str, object] | None:
    """Return a function-tool dict with a resolvable top-level ``name``.

    Memory providers expose tool schemas via ``get_tool_schemas()``. The expected
    shape is a bare function schema (``{"name": ..., "description": ...,
    "parameters": ...}``) which callers wrap as
    ``{"type": "function", "function": schema}``.

    Some providers instead return an entry that is *already* in OpenAI tool form
    (``{"type": "function", "function": {"name": ...}}``). Wrapping that a second
    time produces ``{"type": "function", "function": {"type": "function",
    "function": {...}}}`` whose ``function`` has no top-level ``name``. Strict
    providers reject the *entire* request with ``tools[N].function: missing field
    name`` (HTTP 400), so one bad schema disables the whole toolset and breaks
    every turn.

    This helper normalizes both shapes to the bare function schema and returns
    ``None`` for anything without a resolvable name, so callers can skip with a
    warning rather than appending a nameless tool.
    """
    if not isinstance(schema, Mapping):
        return None
    candidate: Mapping[str, object] = schema
    function = schema.get("function")
    # Unwrap an already-wrapped OpenAI tool entry.
    if schema.get("type") == "function" and isinstance(function, Mapping):
        candidate = function
    name = candidate.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    return dict(candidate)


def memory_provider_tools_enabled(
    enabled_toolsets: Sequence[str] | None,
    disabled_toolsets: Sequence[str] | None = None,
    *,
    memory_tool_present: bool = False,
) -> bool:
    """Return whether memory-provider tools should be exposed.

    hermes resolves toolset aliases through ``toolsets.resolve_toolset``; Run
    Agent has no toolset registry, so this simplified form reads the literal
    ``"memory"`` name from the enabled/disabled lists. An explicitly disabled
    ``memory`` always wins, and a provider tool is exposed when the built-in
    memory tool is already present, when no toolset filter is configured, or when
    ``"memory"`` is explicitly enabled.
    """
    if disabled_toolsets and "memory" in disabled_toolsets:
        return False
    if memory_tool_present:
        return True
    if enabled_toolsets is None:
        return True
    if not enabled_toolsets:
        return False
    return "memory" in enabled_toolsets


# ---------------------------------------------------------------------------
# Context fencing helpers
# ---------------------------------------------------------------------------

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_INTERNAL_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*"
    r"Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*",
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks and system notes from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub("", text)
    text = _INTERNAL_NOTE_RE.sub("", text)
    text = _FENCE_TAG_RE.sub("", text)
    return text


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in a fenced block with a system note.

    Returns ``""`` for empty/blank input. Provider output that already carries
    its own fence (or the system note) is stripped first and a warning is logged,
    so a double-wrapped provider cannot nest one fence inside another and confuse
    the streaming scrubber.
    """
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    if clean != raw_context:
        logger.warning("memory provider returned pre-wrapped context; stripped")
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split memory-context spans.

    The one-shot :func:`sanitize_context` regex cannot survive chunk boundaries: a
    ``<memory-context>`` opened in one delta and closed in a later delta leaks its
    payload to the UI because the non-greedy block regex needs both tags in one
    string. This scrubber runs a small state machine across deltas, holding back
    partial-tag tails and discarding everything inside a span (including the
    system-note line).

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()  # at end of stream
        if trailing:
            emit(trailing)

    The scrubber is re-entrant per agent instance. Callers building new top-level
    responses (new turn) should create a fresh scrubber or call ``reset()``.
    """

    _OPEN_TAG = "<memory-context>"
    _CLOSE_TAG = "</memory-context>"

    def __init__(self) -> None:
        self._in_span: bool = False
        self._buf: str = ""
        self._at_block_boundary: bool = True

    def reset(self) -> None:
        """Return to the start-of-stream state, discarding held-back text."""
        self._in_span = False
        self._buf = ""
        self._at_block_boundary = True

    def feed(self, text: str) -> str:
        """Return the visible portion of ``text`` after scrubbing.

        Any trailing fragment that could be the start of an open/close tag is
        held back in the internal buffer and surfaced on the next ``feed()`` call
        or emitted/discarded by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                idx = buf.lower().find(self._CLOSE_TAG)
                if idx == -1:
                    # Hold back a potential partial close tag; drop the rest.
                    held = self._max_partial_suffix(buf, self._CLOSE_TAG)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # Found the close — skip span content + tag, continue.
                buf = buf[idx + len(self._CLOSE_TAG) :]
                self._in_span = False
            else:
                idx = self._find_boundary_open_tag(buf)
                if idx == -1:
                    # No open tag — hold back a potential partial open tag.
                    held = self._max_pending_open_suffix(buf) or self._max_partial_suffix(
                        buf, self._OPEN_TAG
                    )
                    if held:
                        self._append_visible(out, buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        self._append_visible(out, buf)
                    return "".join(out)
                # Emit text before the tag, enter the span.
                if idx > 0:
                    self._append_visible(out, buf[:idx])
                buf = buf[idx + len(self._OPEN_TAG) :]
                self._in_span = True

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        If we are still inside an unterminated span the remaining content is
        discarded (safer: leaking partial memory context is worse than a truncated
        answer). Otherwise the held-back partial-tag tail is emitted verbatim —
        it turned out not to be a real tag.
        """
        if self._in_span:
            self._buf = ""
            self._in_span = False
            return ""
        tail = self._buf
        self._buf = ""
        return tail

    @staticmethod
    def _max_partial_suffix(buf: str, tag: str) -> int:
        """Return the length of the longest buf-suffix that is a tag-prefix.

        Case-insensitive. Returns 0 if no suffix could start the tag.
        """
        tag_lower = tag.lower()
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), len(tag_lower) - 1)
        for i in range(max_check, 0, -1):
            if tag_lower.startswith(buf_lower[-i:]):
                return i
        return 0

    def _find_boundary_open_tag(self, buf: str) -> int:
        """Find an opening fence only when it starts a block-like span."""
        buf_lower = buf.lower()
        search_start = 0
        while True:
            idx = buf_lower.find(self._OPEN_TAG, search_start)
            if idx == -1:
                return -1
            if self._is_block_boundary(buf, idx) and self._has_block_opener_suffix(buf, idx):
                return idx
            search_start = idx + 1

    def _max_pending_open_suffix(self, buf: str) -> int:
        """Hold a complete boundary tag until the following char confirms it."""
        if not buf.lower().endswith(self._OPEN_TAG):
            return 0
        idx = len(buf) - len(self._OPEN_TAG)
        if not self._is_block_boundary(buf, idx):
            return 0
        return len(self._OPEN_TAG)

    def _has_block_opener_suffix(self, buf: str, idx: int) -> bool:
        after_idx = idx + len(self._OPEN_TAG)
        if after_idx >= len(buf):
            return False
        return buf[after_idx] in "\r\n"

    def _is_block_boundary(self, buf: str, idx: int) -> bool:
        if idx == 0:
            return self._at_block_boundary
        preceding = buf[:idx]
        last_newline = preceding.rfind("\n")
        if last_newline == -1:
            return self._at_block_boundary and preceding.strip() == ""
        return preceding[last_newline + 1 :].strip() == ""

    def _append_visible(self, out: list[str], text: str) -> None:
        if not text:
            return
        out.append(text)
        self._update_block_boundary(text)

    def _update_block_boundary(self, text: str) -> None:
        last_newline = text.rfind("\n")
        if last_newline != -1:
            self._at_block_boundary = text[last_newline + 1 :].strip() == ""
        else:
            self._at_block_boundary = self._at_block_boundary and text.strip() == ""


__all__ = [
    "INDICATOR_GLYPH",
    "TRIVIAL_PROMPT_RE",
    "MemoryProvider",
    "RecallStatus",
    "StreamingContextScrubber",
    "build_memory_context_block",
    "is_trivial_prompt",
    "memory_provider_tools_enabled",
    "normalize_tool_schema",
    "sanitize_context",
]
