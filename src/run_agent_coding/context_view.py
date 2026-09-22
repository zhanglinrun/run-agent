"""Protocol-aware cheap-first views over an immutable session transcript."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from run_agent_coding.context_window import (
    COMPACTION_SUMMARY_PREFIX as REPLAY_SUMMARY_PREFIX,
)
from run_agent_coding.context_window import (
    estimate_context_usage,
    estimate_message_tokens,
)
from run_agent_core.messages import (
    COMPACTION_SUMMARY_PREFIX as PROVIDER_SUMMARY_PREFIX,
)
from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
    message_text,
)
from run_agent_core.provider import ModelRequest

ContextStrategy = Literal["cheap-first", "summary-only"]

# A compaction replay writes "Previous conversation summary:"; a user message that
# already carries the provider-native summary wrapper is a persisted prefix as well.
# Both are stable while a tail grows, unlike a user request that a later turn replaces.
_SUMMARY_PREFIXES = (REPLAY_SUMMARY_PREFIX, PROVIDER_SUMMARY_PREFIX)


class ContextBudgetExceeded(RuntimeError):
    """The request cannot fit the model window without a persisted summary."""


@dataclass(frozen=True, slots=True)
class ContextArtifact:
    digest: str
    relative_path: str
    original_chars: int
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """The frozen Provider view plus the report of how it was produced.

    ``stable_prefix_digest`` identifies the provider prefix that a caller may
    cache: the system prompt plus the head anchor of the conversation (a
    persisted compaction summary, otherwise the first user request). See
    ``_stable_prefix_digest`` for the exact definition and its invariants.
    """

    request: ModelRequest
    tokens_before: int
    tokens_after: int
    layers: tuple[str, ...]
    artifacts: tuple[ContextArtifact, ...]
    stable_prefix_digest: str
    needs_l4: bool


class ContextViewPipeline:
    """Create a bounded Provider view without rewriting durable messages."""

    def __init__(
        self,
        *,
        cwd: Path,
        context_window_tokens: int,
        reserve_tokens: int,
        strategy: ContextStrategy = "cheap-first",
        spill_chars: int = 20_000,
        spill_preview_chars: int = 2_000,
        keep_recent_tokens: int = 20_000,
        compact_result_chars: int = 200,
        keep_recent_results: int = 5,
    ) -> None:
        if context_window_tokens < 1 or reserve_tokens < 0:
            raise ValueError("Context window must be positive and reserve must be non-negative")
        if strategy not in {"cheap-first", "summary-only"}:
            raise ValueError(f"Unknown context strategy: {strategy}")
        self.cwd = cwd.resolve()
        self.context_window_tokens = context_window_tokens
        self.reserve_tokens = reserve_tokens
        self.strategy = strategy
        self.spill_chars = spill_chars
        self.spill_preview_chars = spill_preview_chars
        self.keep_recent_tokens = keep_recent_tokens
        self.compact_result_chars = compact_result_chars
        self.keep_recent_results = keep_recent_results

    @property
    def target_tokens(self) -> int:
        return max(1, self.context_window_tokens - self.reserve_tokens)

    def prepare(self, request: ModelRequest, session_id: str | None = None) -> PreparedContext:
        """Return a detached Provider request and a deterministic transformation report."""
        del session_id  # Reserved for future per-session cache accounting.
        original = tuple(message.model_copy(deep=True) for message in request.messages)
        before = self._tokens(request, original)
        stable_digest = _stable_prefix_digest(request.system, original)
        if before <= self.target_tokens or self.strategy == "summary-only":
            copied = replace(request, messages=original)
            return PreparedContext(
                copied, before, before, (), (), stable_digest, before > self.target_tokens
            )

        current = original
        layers: list[str] = []
        artifacts: tuple[ContextArtifact, ...] = ()

        current, artifacts = self._spill_large_results(current)
        if artifacts:
            layers.append("L3")

        current, folded = self._fold_middle_turns(current, artifacts)
        if folded:
            layers.append("L1")

        current, compacted = self._compact_old_results(current)
        if compacted:
            layers.append("L2")

        after = self._tokens(request, current)
        prepared = replace(request, messages=current)
        return PreparedContext(
            prepared,
            before,
            after,
            tuple(layers),
            artifacts,
            stable_digest,
            after > self.target_tokens,
        )

    def require_hard_limit(self, prepared: PreparedContext) -> None:
        """Refuse a physical request that still exceeds the model's hard window."""
        if prepared.tokens_after > self.context_window_tokens:
            raise ContextBudgetExceeded(
                f"context view needs {prepared.tokens_after} tokens but the model window is "
                f"{self.context_window_tokens}; persistent L4 compaction is required"
            )

    def _tokens(self, request: ModelRequest, messages: tuple[AgentMessage, ...]) -> int:
        return estimate_context_usage(
            system=request.system,
            messages=messages,
            tools=tuple(request.tools),
        ).total_tokens

    def _spill_large_results(
        self, messages: tuple[AgentMessage, ...]
    ) -> tuple[tuple[AgentMessage, ...], tuple[ContextArtifact, ...]]:
        rewritten: list[AgentMessage] = []
        artifacts: list[ContextArtifact] = []
        for message in messages:
            if not isinstance(message, ToolResultMessage) or len(message.text) <= self.spill_chars:
                rewritten.append(message)
                continue
            raw = message.text.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            relative = Path(".run") / "context" / "blobs" / f"{digest}.txt"
            target = self.cwd / relative
            _write_blob_once(target, raw)
            artifacts.append(
                ContextArtifact(
                    digest=digest,
                    relative_path=relative.as_posix(),
                    original_chars=len(message.text),
                    tool_call_id=message.tool_call_id,
                )
            )
            preview = message.text[: self.spill_preview_chars]
            text = (
                "<persisted-tool-result "
                f'sha256="{digest}" chars="{len(message.text)}" '
                f'path="{relative.as_posix()}">\n{preview}\n</persisted-tool-result>'
            )
            rewritten.append(message.model_copy(update={"content": [TextContent(text=text)]}))
        return tuple(rewritten), tuple(artifacts)

    def _fold_middle_turns(
        self,
        messages: tuple[AgentMessage, ...],
        artifacts: tuple[ContextArtifact, ...],
    ) -> tuple[tuple[AgentMessage, ...], bool]:
        turns = _turns(messages)
        if len(turns) <= 2:
            return messages, False

        keep_from = len(turns) - 1
        recent_tokens = 0
        for index in range(len(turns) - 1, 0, -1):
            turn_tokens = sum(estimate_message_tokens(message) for message in turns[index])
            if recent_tokens and recent_tokens + turn_tokens > self.keep_recent_tokens:
                break
            recent_tokens += turn_tokens
            keep_from = index

        removed = turns[1:keep_from]
        if not removed:
            return messages, False
        artifact_by_call = {item.tool_call_id: item.digest for item in artifacts}
        checkpoint = UserMessage(content=_folded_checkpoint(removed, artifact_by_call))
        flattened = [*turns[0], checkpoint]
        for turn in turns[keep_from:]:
            flattened.extend(turn)
        return tuple(flattened), True

    def _compact_old_results(
        self, messages: tuple[AgentMessage, ...]
    ) -> tuple[tuple[AgentMessage, ...], int]:
        result_positions = [
            index for index, item in enumerate(messages) if isinstance(item, ToolResultMessage)
        ]
        old_positions = set(result_positions[: -self.keep_recent_results or None])
        changed = 0
        rewritten: list[AgentMessage] = []
        for index, message in enumerate(messages):
            if (
                not isinstance(message, ToolResultMessage)
                or index not in old_positions
                or len(message.text) <= self.compact_result_chars
            ):
                rewritten.append(message)
                continue
            digest = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
            preview = message.text[: self.compact_result_chars]
            text = (
                f"[Earlier {message.tool_name} result compacted: sha256={digest}, "
                f"chars={len(message.text)}]\n{preview}"
            )
            rewritten.append(message.model_copy(update={"content": [TextContent(text=text)]}))
            changed += 1
        return tuple(rewritten), changed


def _turns(messages: tuple[AgentMessage, ...]) -> list[list[AgentMessage]]:
    """Group complete user turns while keeping assistant/tool blocks inseparable."""
    turns: list[list[AgentMessage]] = []
    current: list[AgentMessage] = []
    for message in messages:
        if isinstance(message, UserMessage) and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def _folded_checkpoint(turns: list[list[AgentMessage]], artifact_by_call: dict[str, str]) -> str:
    rows = [f'<folded-context turns="{len(turns)}">']
    for index, turn in enumerate(turns, start=1):
        user = next((message.text for message in turn if isinstance(message, UserMessage)), "")
        assistants = [message for message in turn if isinstance(message, AssistantMessage)]
        final = assistants[-1].text if assistants else ""
        tools = []
        for message in turn:
            if isinstance(message, ToolResultMessage):
                digest = artifact_by_call.get(message.tool_call_id)
                suffix = f":{digest}" if digest else ""
                tools.append(f"{message.tool_name}:{'error' if message.is_error else 'ok'}{suffix}")
        rows.append(f"turn {index} user: {user[:500]}")
        if tools:
            rows.append("tools: " + ", ".join(tools))
        if final:
            rows.append(f"assistant: {final[:500]}")
    rows.append("</folded-context>")
    return "\n".join(rows)


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
    * A new L4 compaction (or a manual ``/compact``) rewrites the head with a
      new summary text, so the digest changes. Re-summarizing into
      byte-identical text provably leaves the provider prefix unchanged, so
      the digest stays equal on purpose: it names the prefix, not the number
      of compaction events.
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


@dataclass(slots=True)
class ContextBlobDiagnostics:
    """Counters for blob writes that are deliberately tolerated.

    ``os.link``/``os.replace`` can lose a same-directory temporary file to an
    external cleaner or a filesystem race, so the write is retried once instead of
    failing the model request. Every caller that uses the default sink can read these
    counters; tests inject their own instance.
    """

    attempts: int = 0
    retries: int = 0
    failures: int = 0
    last_failure_path: str | None = None
    last_failure_error: str | None = None

    def record_attempt(self) -> None:
        self.attempts += 1

    def record_retry(self) -> None:
        self.retries += 1

    def record_failure(self, path: Path, error: OSError) -> None:
        self.failures += 1
        self.last_failure_path = str(path)
        self.last_failure_error = f"{type(error).__name__}: {error}"


DEFAULT_CONTEXT_BLOB_DIAGNOSTICS = ContextBlobDiagnostics()


def context_blob_diagnostics() -> ContextBlobDiagnostics:
    """Return the process-wide sink used by blob writes that were given no sink."""
    return DEFAULT_CONTEXT_BLOB_DIAGNOSTICS


def _write_blob_once(
    path: Path, data: bytes, *, diagnostics: ContextBlobDiagnostics | None = None
) -> None:
    """Write one content-addressed blob, retrying once if its temporary file vanishes.

    An existing target with identical bytes is success and different bytes are a
    digest collision. A vanished temporary file - ``FileNotFoundError`` from
    ``os.link`` or from the ``os.replace`` fallback - is rebuilt with a fresh
    temporary file: same directory, same fsync, same atomic link/replace. A repeated
    failure is recorded on the diagnostics sink and then propagates.
    """
    sink = diagnostics if diagnostics is not None else DEFAULT_CONTEXT_BLOB_DIAGNOSTICS
    for attempt in (1, 2):
        sink.record_attempt()
        try:
            _write_blob_attempt(path, data)
        except FileNotFoundError as exc:
            if attempt == 2:
                sink.record_failure(path, exc)
                raise
            sink.record_retry()
        else:
            return


def _write_blob_attempt(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.read_bytes() != data:
            raise RuntimeError(f"context blob digest collision at {path}")
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError(f"context blob digest collision at {path}") from None
        except OSError:
            if not path.exists():
                os.replace(temporary, path)
        if path.exists():
            try:
                directory = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                except OSError:
                    pass
                finally:
                    os.close(directory)
    finally:
        with suppress(OSError):
            Path(temporary).unlink()


__all__ = [
    "ContextArtifact",
    "ContextBlobDiagnostics",
    "ContextBudgetExceeded",
    "ContextStrategy",
    "ContextViewPipeline",
    "PreparedContext",
    "context_blob_diagnostics",
]
