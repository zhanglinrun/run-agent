"""Anthropic-compatible normalization helpers."""

from __future__ import annotations

from typing import Any

from ..runtime.contracts import ToolCall
from .base import ModelRequest, ModelResponse


def _safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def normalize_anthropic_tool_call(block: Any) -> ToolCall:
    value = getattr(block, "input", {})
    if not hasattr(value, "items"):
        return ToolCall(
            id=str(getattr(block, "id", "")),
            name=str(getattr(block, "name", "")),
            input={
                "__tool_input_error__": "Anthropic tool input must be an object"
            },
        )
    return ToolCall(
        id=str(getattr(block, "id", "")),
        name=str(getattr(block, "name", "")),
        input=dict(value),
    )


class AnthropicProviderAdapter:
    """Provider adapter that normalizes Messages API responses for AgentCore."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float | None = None,
        thinking: bool = False,
    ) -> None:
        import anthropic
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)
        self.model = model
        self.temperature = temperature
        self.thinking = thinking

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        system_parts = [request.system] if request.system else []
        pending_tool_results: list[dict[str, Any]] = []
        for raw in request.messages:
            role = str(raw.get("role") or "user")
            if role == "system":
                content = _safe_text(raw.get("content", "")).strip()
                if content:
                    system_parts.append(content)
                continue
            if role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": str(raw.get("tool_call_id") or ""),
                    "content": _safe_text(raw.get("content", "")),
                })
                continue
            if pending_tool_results:
                messages.append({"role": "user", "content": pending_tool_results})
                pending_tool_results = []
            if role == "assistant" and raw.get("tool_calls"):
                content: list[dict[str, Any]] = []
                if raw.get("content"):
                    content.append({"type": "text", "text": _safe_text(raw["content"])})
                for item in raw["tool_calls"]:
                    function = item.get("function") or {}
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            import json
                            arguments = json.loads(arguments)
                        except Exception:
                            arguments = {}
                    content.append({"type": "tool_use", "id": str(item.get("id") or ""), "name": str(function.get("name") or ""), "input": arguments})
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": "assistant" if role == "assistant" else "user", "content": raw.get("content", "")})
        if pending_tool_results:
            messages.append({"role": "user", "content": pending_tool_results})
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
            "tools": [{"name": item["name"], "description": item.get("description", ""), "input_schema": item.get("input_schema", {})} for item in request.tools],
        }
        if system_parts:
            params["system"] = "\n\n".join(system_parts)
        if self.thinking:
            params["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        elif self.temperature is not None:
            params["temperature"] = self.temperature
        response = await self.client.messages.create(**params)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in list(getattr(response, "content", []) or []):
            block_type = str(getattr(block, "type", ""))
            if block_type == "text":
                text_parts.append(_safe_text(getattr(block, "text", "")))
            elif block_type == "tool_use":
                calls.append(normalize_anthropic_tool_call(block))
        usage_obj = getattr(response, "usage", None)
        usage = {"input": int(getattr(usage_obj, "input_tokens", 0) or 0), "output": int(getattr(usage_obj, "output_tokens", 0) or 0)}
        return ModelResponse(text="".join(text_parts), tool_calls=tuple(calls), stop_reason=str(getattr(response, "stop_reason", "") or ""), usage=usage, raw=response)
