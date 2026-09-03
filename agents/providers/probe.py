"""Small preflight probes for live campaign configuration."""

from __future__ import annotations

from typing import Any


async def probe_model(
    *,
    model: str,
    api_key: str,
    base_url: str | None,
    use_openai: bool,
) -> dict[str, Any]:
    """Verify reachability, tool calling and usage reporting before a campaign."""
    if use_openai:
        import openai

        client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        token_limit = (
            {"max_completion_tokens": 64}
            if model.lower().startswith("gpt-5")
            else {"max_tokens": 64}
        )
        request: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Call the probe tool now. Do not answer with text."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "probe",
                    "description": "Campaign preflight probe",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            **token_limit,
        }
        # DeepSeek Thinking mode rejects explicit tool_choice values. With a
        # single tool and an explicit instruction, auto selection still gives
        # a deterministic tool-calling preflight.
        if not model.lower().startswith("deepseek-"):
            request["tool_choice"] = {"type": "function", "function": {"name": "probe"}}
        response = await client.chat.completions.create(**request)
        choices = list(getattr(response, "choices", []) or [])
        if not choices:
            raise RuntimeError("model probe returned no choices")
        message = getattr(choices[0], "message", None)
        tool_calls = list(getattr(message, "tool_calls", []) or []) if message is not None else []
        usage = getattr(response, "usage", None)
        if usage is None:
            raise RuntimeError("model probe response did not include token usage")
        if not tool_calls:
            raise RuntimeError("model probe did not return a tool call")
        return {
            "ok": True,
            "protocol": "openai",
            "model": str(getattr(response, "model", model) or model),
            "tool_call_supported": bool(tool_calls),
            "usage_supported": True,
        }

    import anthropic

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = anthropic.AsyncAnthropic(**kwargs)
    response = await client.messages.create(
        model=model,
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with a tool call if tools are supported."}],
        tools=[{
            "name": "probe",
            "description": "Campaign preflight probe",
            "input_schema": {"type": "object", "properties": {}},
        }],
        tool_choice={"type": "tool", "name": "probe"},
    )
    blocks = list(getattr(response, "content", []) or [])
    usage = getattr(response, "usage", None)
    if usage is None:
        raise RuntimeError("model probe response did not include token usage")
    if not any(getattr(block, "type", "") == "tool_use" for block in blocks):
        raise RuntimeError("model probe did not return a tool call")
    return {
        "ok": True,
        "protocol": "anthropic",
        "model": str(getattr(response, "model", model) or model),
        "tool_call_supported": True,
        "usage_supported": True,
    }


__all__ = ["probe_model"]
