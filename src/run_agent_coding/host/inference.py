"""One bounded completion an extension may ask the host to run.

The dividing line: a caller freezes *what* it wants answered - the prompt and the
system instruction - while the host owns *how* it is answered, meaning the provider,
the model, the transcript isolation and the recording of the exact input. A caller
may select existing tool schemas by name, but cannot supply executors, a session
handle or a persisted transcript. The host returns proposed tool calls as data and
never executes them. The caller must apply its own allowlist and write guards.

Two refusals keep it from becoming a second front door to the provider:

``InferenceUnavailable``
    No provider is reachable, so nothing can be answered. Reporting an empty answer
    instead would let a caller record a conclusion it never measured.
``InferenceBusy``
    A foreground run is in flight. The host's own review path defers to the user's
    turn rather than competing with it, and this is where that promise is enforced
    instead of merely intended.

``UnavailableInference`` is the default for a host that composed no provider, so a
caller keeps its candidate pending rather than inventing evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from run_agent_coding.thinking import THINKING_LEVELS, ThinkingLevel
from run_agent_core.messages import ToolCall

# Purposes are recorded in the committed input payload, so they are bounded the way
# every other recorded identity in this host is.
MAX_PURPOSE_BYTES = 128


class InferenceUnavailable(RuntimeError):
    """Raised when this host has no provider that can answer an inference request."""


class InferenceBusy(RuntimeError):
    """Raised when a foreground run is in flight and would be competed with."""


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """One frozen completion request. The host decides the provider and the model."""

    prompt: str
    system: str = ""
    purpose: str = "extension"
    thinking_level: ThinkingLevel | None = None
    max_output_tokens: int | None = None
    tool_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tool_names, tuple)
            or any(not isinstance(name, str) or not name.strip() for name in self.tool_names)
            or len(set(self.tool_names)) != len(self.tool_names)
        ):
            raise ValueError("Inference tool_names must contain unique non-empty tool names")
        if self.thinking_level is not None and self.thinking_level not in THINKING_LEVELS:
            raise ValueError(f"Unknown inference thinking level: {self.thinking_level!r}")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens < 1
        ):
            raise ValueError("Inference max_output_tokens must be a positive integer")
        if not self.prompt.strip():
            raise ValueError("An inference request needs a non-empty prompt")
        if not self.purpose or len(self.purpose.encode()) > MAX_PURPOSE_BYTES:
            raise ValueError(
                f"An inference purpose must contain 1 to {MAX_PURPOSE_BYTES} UTF-8 bytes"
            )


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """What one completion returned, and the committed input it answers.

    Token counts are what the provider reported, and stay ``0`` when it reported
    nothing: a caller attributing spend needs to see the difference between a
    provider that charged nothing and one that said nothing.
    """

    text: str
    model: str
    snapshot_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: tuple[ToolCall, ...] = ()


class InferenceService(Protocol):
    """Host-owned completions. Requests are frozen and never enter the transcript."""

    @property
    def available(self) -> bool:
        """Whether this host can actually reach a provider right now."""
        ...

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        """Answer one request, or refuse with ``InferenceUnavailable``/``InferenceBusy``."""
        ...


class UnavailableInference:
    """The default for a host with no provider composed into it."""

    @property
    def available(self) -> bool:
        """Always false; nothing can be answered here."""
        return False

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        """Refuse, so the caller keeps its work pending instead of inventing an answer."""
        raise InferenceUnavailable(
            f"no provider is registered for the inference purpose {request.purpose!r}"
        )
