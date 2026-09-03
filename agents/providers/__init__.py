"""Protocol adapters that normalize provider-specific tool calls."""

from .anthropic import AnthropicProviderAdapter, normalize_anthropic_tool_call
from .base import ModelRequest, ModelResponse, ProviderAdapter
from .openai import OpenAIProviderAdapter, decode_openai_tool_arguments, to_openai_tools
from .probe import probe_model

__all__ = [
    "ModelRequest",
    "ModelResponse",
    "ProviderAdapter",
    "decode_openai_tool_arguments",
    "normalize_anthropic_tool_call",
    "AnthropicProviderAdapter",
    "OpenAIProviderAdapter",
    "to_openai_tools",
    "probe_model",
]
