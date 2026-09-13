"""Channel adapters: the shared base and the Feishu implementation."""

from run_agent_gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageHandler,
    SendResult,
    split_message,
)

__all__ = ["BasePlatformAdapter", "MessageEvent", "MessageHandler", "SendResult", "split_message"]
