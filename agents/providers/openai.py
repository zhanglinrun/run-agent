"""OpenAI-compatible normalization helpers."""

from __future__ import annotations

import json
from typing import Any

from .base import ModelRequest, ModelResponse
from ..runtime.contracts import ToolCall


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert provider-neutral tool definitions to Chat Completions format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


def decode_openai_tool_arguments(raw: Any) -> dict[str, Any]:
    """Decode tool arguments without silently converting malformed JSON to {}."""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except Exception as exc:
        return {"__tool_input_error__": f"invalid tool argument JSON: {exc}"}
    if not isinstance(value, dict):
        return {"__tool_input_error__": "tool arguments must decode to a JSON object"}
    return value


class OpenAIProviderAdapter:
    """Provider adapter used by the provider-neutral AgentCore."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float | None = None,
        thinking: bool = False,
    ) -> None:
        import openai
        self.model = model
        self.temperature = temperature
        self.thinking = thinking
        self.client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for raw in request.messages:
            message = dict(raw)
            if message.get("role") == "assistant" and message.get("tool_calls"):
                normalized = []
                for item in message["tool_calls"]:
                    function = dict(item.get("function") or {})
                    arguments = function.get("arguments", {})
                    function["arguments"] = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
                    normalized.append({**item, "function": function})
                message["tool_calls"] = normalized
            messages.append(message)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": to_openai_tools(list(request.tools)),
        }
        if self.thinking:
            params["reasoning_effort"] = "high"
        elif self.temperature is not None:
            params["temperature"] = self.temperature
        params["max_completion_tokens" if self.model.lower().startswith("gpt-5") else "max_tokens"] = 4096
        response = await self.client.chat.completions.create(**params)
        choice = response.choices[0] if response.choices else None
        message = getattr(choice, "message", None)
        calls: list[ToolCall] = []
        for item in list(getattr(message, "tool_calls", None) or []):
            function = getattr(item, "function", None)
            calls.append(ToolCall(
                str(getattr(item, "id", "")),
                str(getattr(function, "name", "")),
                decode_openai_tool_arguments(getattr(function, "arguments", "{}")),
            ))
        usage_obj = getattr(response, "usage", None)
        usage = {
            "input": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "output": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        }
        return ModelResponse(
            text=str(getattr(message, "content", "") or ""),
            tool_calls=tuple(calls),
            stop_reason=str(getattr(choice, "finish_reason", "") or ""),
            usage=usage,
            raw=response,
        )
