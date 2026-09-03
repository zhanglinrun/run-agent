"""Provider-neutral AgentCore, contracts, hooks, and trace recording."""

from .contracts import EventType, ToolCall, ToolResult
from .core import AgentCore, AgentCoreResult, ToolExecutor
from .events import EventBus
from .hooks import AgentHooks, CompositeAgentHooks, ModelContext, NextTurnDecision, ToolCallDecision, TurnResult
from .scope import bind_workspace, current_workspace
from .tracing import TraceRecorder

__all__ = [
    "AgentCore",
    "AgentCoreResult",
    "AgentHooks",
    "CompositeAgentHooks",
    "EventBus",
    "EventType",
    "ToolCall",
    "ToolResult",
    "ToolExecutor",
    "ModelContext",
    "NextTurnDecision",
    "ToolCallDecision",
    "TurnResult",
    "TraceRecorder",
    "bind_workspace",
    "current_workspace",
]
