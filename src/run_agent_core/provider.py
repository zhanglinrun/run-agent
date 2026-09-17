"""Provider contract owned by Run Agent's portable agent layer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol

from run_agent_core.messages import AgentMessage
from run_agent_core.provider_events import AssistantMessageEvent
from run_agent_core.tools import AgentTool


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """The actual input after context transformation and history repair."""

    model: str
    system: str
    messages: Sequence[AgentMessage]
    tools: Sequence[AgentTool]
    session_id: str | None = None


BeforeModelRequest = Callable[[ModelRequest], Awaitable["ModelRequest | None"]]


class ProviderHttpHooks(Protocol):
    """Optional HTTP-layer hooks applied around a physical provider call."""

    async def prepare_provider_headers(self, headers: dict[str, str]) -> None: ...

    async def observe_provider_response(
        self, status: int, headers: Mapping[str, str]
    ) -> None: ...


_PROVIDER_HTTP_HOOKS: ContextVar[ProviderHttpHooks | None] = ContextVar(
    "run_agent_provider_http_hooks", default=None
)


def bind_provider_http_hooks(hooks: ProviderHttpHooks | None) -> Token[ProviderHttpHooks | None]:
    """Bind HTTP hooks for the current task; return a token to restore later."""
    return _PROVIDER_HTTP_HOOKS.set(hooks)


async def run_before_provider_headers(headers: dict[str, str]) -> None:
    """Apply in-place header mutations from the bound extension runtime, if any."""
    hooks = _PROVIDER_HTTP_HOOKS.get()
    if hooks is None:
        return
    await hooks.prepare_provider_headers(headers)


async def run_after_provider_response(status: int, headers: Mapping[str, str]) -> None:
    """Notify observers of a provider HTTP response, if a runtime is bound."""
    hooks = _PROVIDER_HTTP_HOOKS.get()
    if hooks is None:
        return
    await hooks.observe_provider_response(status, headers)


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the current stream should stop."""
        ...


class ModelProvider(Protocol):
    """Provider-neutral Pi-compatible model stream interface."""

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Stream one model response as assistant message events.

        Providers may use ``session_id`` for request routing or prompt-cache
        affinity. Unsupported providers ignore it.
        """
        ...
