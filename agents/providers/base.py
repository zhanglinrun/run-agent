"""Provider-neutral request/response interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..runtime.contracts import ToolCall


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    system: str = ""


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class ProviderAdapter(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
