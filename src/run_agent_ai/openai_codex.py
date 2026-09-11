"""OpenAI Codex subscription Responses provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError, dumps, loads
from platform import machine, release, system
from typing import Any

import httpx

from run_agent_ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from run_agent_ai.content import (
    NON_VISION_TOOL_IMAGE_PLACEHOLDER,
    NON_VISION_USER_IMAGE_PLACEHOLDER,
    text_and_images,
)
from run_agent_ai.env import (
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
)
from run_agent_ai.events import AssistantMessageEvent
from run_agent_ai.http import create_async_client
from run_agent_ai.http_errors import provider_http_error_message
from run_agent_ai.model_limits import RuntimeModelLimits
from run_agent_ai.openai_cache import openai_prompt_cache_key
from run_agent_ai.provider import CancellationToken
from run_agent_ai.retry import provider_retry_event, retry_delay_seconds, wait_for_retry
from run_agent_ai.stream import canonicalize_provider_stream
from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolResultMessage,
    Usage,
    UserMessage,
    assistant_content,
)
from run_agent_core.tools import AgentTool, ToolCall
from run_agent_core.types import JSONValue

DEFAULT_OPENAI_CODEX_BASE_URL = "https://chatgpt.com/backend-api"


@dataclass(frozen=True, slots=True)
class OpenAICodexCredentials:
    """Bearer token and account id required by ChatGPT Codex Responses."""

    access_token: str
    account_id: str


type OpenAICodexCredentialResolver = Callable[[], Awaitable[OpenAICodexCredentials]]


@dataclass(frozen=True, slots=True)
class OpenAICodexConfig:
    """Configuration for the OpenAI Codex subscription Responses endpoint."""

    credential_resolver: OpenAICodexCredentialResolver
    base_url: str = DEFAULT_OPENAI_CODEX_BASE_URL
    headers: Mapping[str, str] | None = None
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    originator: str = "run-agent"
    reasoning_effort: str | None = None
    reasoning_summary: str = "auto"
    supports_images: bool = False
    provider_name: str = "OpenAI Codex"
    # The Codex catalog filters models by the official client's compatibility
    # version. This is the oldest known version that advertises GPT-5.6.
    client_version: str = "0.144.3"
    model_catalog_timeout_seconds: float = 5.0


class OpenAICodexProvider:
    """Provider adapter for ChatGPT subscription Codex Responses over SSE."""

    def __init__(
        self,
        config: OpenAICodexConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._discovered_model_limits: dict[str, RuntimeModelLimits] | None = None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def discover_model_limits(self, model: str) -> RuntimeModelLimits | None:
        """Discover model limits from the authenticated Codex model catalog."""
        if self._discovered_model_limits is None:
            self._discovered_model_limits = await self._fetch_model_limits()
        return self._discovered_model_limits.get(model)

    async def _fetch_model_limits(self) -> dict[str, RuntimeModelLimits]:
        client = self._get_client()
        credentials = await self._config.credential_resolver()
        headers = _build_codex_headers(
            self._config.headers,
            access_token=credentials.access_token,
            account_id=credentials.account_id,
            originator=self._config.originator,
        )
        headers["accept"] = "application/json"
        headers.pop("content-type", None)
        response = await client.get(
            _resolve_codex_models_url(self._config.base_url),
            params={"client_version": self._config.client_version},
            headers=headers,
            timeout=self._config.model_catalog_timeout_seconds,
        )
        response.raise_for_status()
        return _parse_codex_model_limits(response.json())

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
        """Stream one response as Pi-compatible assistant message events."""
        raw = self._stream_provider_events(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            session_id=session_id,
        )
        return canonicalize_provider_stream(
            raw, api="openai-codex-responses", provider="openai-codex", model=model
        )

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one Codex Responses request as provider-neutral events."""

        async def iterator() -> AsyncIterator[ProviderEvent]:
            client = self._get_client()
            cache_key = openai_prompt_cache_key(session_id)
            payload = _build_codex_payload(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                reasoning_effort=self._config.reasoning_effort,
                reasoning_summary=self._config.reasoning_summary,
                supports_images=self._config.supports_images,
                prompt_cache_key=cache_key,
            )
            url = _resolve_codex_url(self._config.base_url)

            attempt = 0
            while True:
                emitted_content = False
                emitted_thinking = False
                try:
                    credentials = await self._config.credential_resolver()
                    headers = _build_codex_headers(
                        self._config.headers,
                        access_token=credentials.access_token,
                        account_id=credentials.account_id,
                        originator=self._config.originator,
                        session_id=cache_key,
                    )
                    async with client.stream(
                        "POST",
                        url,
                        json=payload,
                        headers=headers,
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            body_text = body.decode(errors="replace")
                            if self._should_retry(
                                attempt,
                                status_code=response.status_code,
                                body=body_text,
                            ):
                                delay = retry_delay_seconds(
                                    attempt,
                                    max_delay_seconds=self._config.max_retry_delay_seconds,
                                )
                                yield provider_retry_event(
                                    attempt=attempt,
                                    max_retries=self._config.max_retries,
                                    delay_seconds=delay,
                                    reason=f"HTTP {response.status_code}",
                                    data={
                                        "status_code": response.status_code,
                                        "body": body_text,
                                    },
                                )
                                attempt += 1
                                if not await wait_for_retry(delay, signal=signal):
                                    return
                                continue
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._config.provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                            )
                            return

                        yield ProviderResponseStartEvent(model=model)
                        stream_error: dict[str, JSONValue] | None = None
                        async for event in _codex_provider_events(response, signal=signal):
                            if isinstance(
                                event,
                                ProviderTextDeltaEvent | ProviderToolCallEvent,
                            ):
                                emitted_content = True
                            elif isinstance(event, ProviderThinkingDeltaEvent):
                                emitted_thinking = True
                            if (
                                isinstance(event, ProviderErrorEvent)
                                and not emitted_content
                                and not emitted_thinking
                                and self._should_retry(attempt)
                                and _retryable_stream_error_event(event)
                            ):
                                stream_error = _stream_error_event_data(event)
                                break
                            yield event
                        if stream_error is None:
                            return
                        code, _message = _stream_error_details(stream_error)
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason=f"stream error ({code or 'unknown'})",
                            data={"event": stream_error},
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                except httpx.HTTPError as exc:
                    if not emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={"attempts": attempt + 1},
                    )
                    return
                except Exception as exc:  # provider errors are surfaced as events
                    yield ProviderErrorEvent(message=str(exc), data={"attempts": attempt + 1})
                    return

        return iterator()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client

    def _should_retry(
        self,
        attempt: int,
        *,
        status_code: int | None = None,
        body: str = "",
    ) -> bool:
        if attempt >= self._config.max_retries:
            return False
        return status_code is None or _is_retryable_status(status_code, body)


class _ToolCallBuilder:
    def __init__(self, *, call_id: str, item_id: str | None, name: str) -> None:
        self.call_id = call_id
        self.item_id = item_id
        self.name = name
        self.arguments_parts: list[str] = []

    def add_delta(self, delta: str) -> None:
        """Append a streamed tool-argument fragment."""
        self.arguments_parts.append(delta)

    def set_arguments(self, arguments: str) -> None:
        """Replace streamed tool arguments with final provider arguments."""
        self.arguments_parts = [arguments]

    def update_from_item(self, item: Mapping[str, Any]) -> None:
        """Fill in metadata from a completed function-call item."""
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            self.call_id = call_id
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            self.item_id = item_id
        name = item.get("name")
        if isinstance(name, str):
            self.name = name

    def build(self) -> ToolCall:
        """Build a complete Run Agent tool call."""
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        item_id = self.item_id or f"fc_{self.call_id}"
        return ToolCall(
            id=f"{self.call_id}|{item_id}",
            name=self.name,
            arguments=arguments,
        )


def _build_codex_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    reasoning_summary: str = "auto",
    supports_images: bool = False,
    prompt_cache_key: str | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": system or "You are a helpful assistant.",
        "input": _messages_to_responses_input(messages, supports_images=supports_images),
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if prompt_cache_key is not None:
        payload["prompt_cache_key"] = prompt_cache_key
    if reasoning_effort is not None:
        payload["reasoning"] = {
            "effort": reasoning_effort,
            "summary": reasoning_summary,
        }
    if tools:
        payload["tools"] = [_tool_to_codex(tool) for tool in tools]
    return payload


def _messages_to_responses_input(
    messages: list[AgentMessage], *, supports_images: bool = False
) -> list[JSONValue]:
    items: list[JSONValue] = []
    assistant_index = 0
    for message in messages:
        if isinstance(message, UserMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_USER_IMAGE_PLACEHOLDER,
            )
            content: list[JSONValue] = []
            if text:
                content.append({"type": "input_text", "text": text})
            content.extend(_codex_input_image(image) for image in images)
            items.append({"role": "user", "content": content})
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingContent) and block.thinking_signature:
                    try:
                        reasoning_item = loads(block.thinking_signature)
                    except (TypeError, ValueError):
                        reasoning_item = None
                    if isinstance(reasoning_item, dict):
                        items.append(reasoning_item)
                elif isinstance(block, TextContent):
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": block.text,
                                    "annotations": [],
                                }
                            ],
                            "status": "completed",
                            "id": block.text_signature or f"msg_{assistant_index}",
                        }
                    )
                    assistant_index += 1
            for tool_call in message.tool_calls:
                call_id, item_id = _split_tool_call_id(tool_call.id)
                item: dict[str, JSONValue] = {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": tool_call.name,
                    "arguments": dumps(tool_call.arguments),
                }
                if item_id:
                    item["id"] = item_id
                items.append(item)
        elif isinstance(message, ToolResultMessage):
            call_id, _item_id = _split_tool_call_id(message.tool_call_id)
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_TOOL_IMAGE_PLACEHOLDER,
            )
            output: JSONValue
            if images:
                output_parts: list[JSONValue] = []
                if text:
                    output_parts.append({"type": "input_text", "text": text})
                output_parts.extend(_codex_input_image(image) for image in images)
                output = output_parts
            else:
                output = text or "(no tool output)"
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                }
            )
    return items


def _codex_input_image(image: ImageContent) -> dict[str, JSONValue]:
    return {
        "type": "input_image",
        "detail": "auto",
        "image_url": f"data:{image.mime_type};base64,{image.data}",
    }


def _tool_to_codex(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.input_schema),
        "strict": None,
    }


async def _codex_provider_events(
    response: httpx.Response,
    *,
    signal: CancellationToken | None,
) -> AsyncIterator[ProviderEvent]:
    """Stream a Codex response, dispatching each SSE event to its own handler.

    This was 174 lines with ten accumulators, one 140-line loop body and eleven levels of
    nesting. The event types were an if/elif chain, so no single type could be tested
    without driving a whole stream past it, and the four tool-tracking collections were
    threaded through six call sites with identical keyword arguments - the shape where one
    index stops being updated and nothing fails until a tool call arrives under a key
    nobody is watching. Both are now structural: a dispatch table and a tracker.
    """
    state = _CodexStreamState()
    async for event in _iter_sse_objects(response):
        if signal is not None and signal.is_cancelled():
            return
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        for produced in _dispatch_codex(state, event_type, event):
            yield produced
        if state.aborted:
            return
        if state.ended:
            break
    yield state.finish()


@dataclass(slots=True)
class _ToolTracker:
    """The three indexes a stream may use to name the same tool call."""

    active: list[_ToolCallBuilder] = field(default_factory=list)
    by_item_id: dict[str, _ToolCallBuilder] = field(default_factory=dict)
    by_call_id: dict[str, _ToolCallBuilder] = field(default_factory=dict)
    by_output_index: dict[int, _ToolCallBuilder] = field(default_factory=dict)

    def track(self, builder: _ToolCallBuilder, event: Mapping[str, Any]) -> None:
        """Register a builder under every key this event offers."""
        _track_tool_builder(
            builder,
            event,
            active_tools=self.active,
            by_item_id=self.by_item_id,
            by_call_id=self.by_call_id,
            by_output_index=self.by_output_index,
        )

    def for_event(self, event: Mapping[str, Any]) -> _ToolCallBuilder | None:
        """The builder this event refers to, whichever of the three keys it uses."""
        return _tool_builder_for_event(
            event,
            active_tools=self.active,
            by_item_id=self.by_item_id,
            by_call_id=self.by_call_id,
            by_output_index=self.by_output_index,
        )

    def untrack(self, builder: _ToolCallBuilder) -> None:
        """Drop a builder once its call has been built and emitted."""
        _untrack_tool_builder(
            builder,
            active_tools=self.active,
            by_item_id=self.by_item_id,
            by_call_id=self.by_call_id,
            by_output_index=self.by_output_index,
        )


@dataclass(slots=True)
class _CodexStreamState:
    """Everything one Codex stream accumulates between its first and last event."""

    content_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    reasoning_items: dict[str, dict[str, JSONValue]] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    tools: _ToolTracker = field(default_factory=_ToolTracker)
    finish_reason: str | None = None
    usage: Usage | None = None
    ended: bool = False
    aborted: bool = False

    def remember_reasoning(self, item: Mapping[str, Any]) -> None:
        """Keep a reasoning item by id, so its signature survives to the end event."""
        item_id = item.get("id")
        if isinstance(item_id, str):
            self.reasoning_items[item_id] = dict(item)

    def finish(self) -> ProviderResponseEndEvent:
        """The closing event: accumulated text, thinking, tool calls and usage."""
        content = assistant_content("".join(self.content_parts), self.tool_calls)
        if self.thinking_parts:
            content.insert(
                0,
                ThinkingContent(
                    thinking="".join(self.thinking_parts),
                    thinking_signature=self.reasoning_signature(),
                ),
            )
        return ProviderResponseEndEvent(
            message=AssistantMessage(content=content, usage=self.usage or Usage()),
            finish_reason=self.finish_reason,
        )

    def reasoning_signature(self) -> str | None:
        """The first reasoning item, serialized, which is what the API expects back."""
        if not self.reasoning_items:
            return None
        return dumps(next(iter(self.reasoning_items.values())))


_CodexHandler = Callable[["_CodexStreamState", dict[str, JSONValue]], tuple[ProviderEvent, ...]]


def _dispatch_codex(
    state: _CodexStreamState, event_type: str, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    """Route one SSE event to its handler; types we do not recognize are ignored."""
    handler = _CODEX_HANDLERS.get(event_type)
    return () if handler is None else handler(state, event)


def _event_item(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The ``item`` object on an event, when it carries one."""
    item = event.get("item")
    return item if isinstance(item, Mapping) else None


def _on_codex_error(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    state.aborted = True
    return (
        ProviderErrorEvent(
            message=_error_message(event, fallback="OpenAI Codex returned an error"),
            data={"event": event},
        ),
    )


def _on_codex_failed(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    state.aborted = True
    return (ProviderErrorEvent(message=_response_error_message(event), data={"event": event}),)


def _on_item_added(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    """A reasoning item is kept for its signature; a function call starts a builder."""
    item = _event_item(event)
    if item is None:
        return ()
    if item.get("type") == "reasoning":
        state.remember_reasoning(item)
    elif item.get("type") == "function_call":
        state.tools.track(_tool_builder_from_item(item), event)
    return ()


def _on_arguments_delta(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    builder = state.tools.for_event(event)
    delta = event.get("delta")
    if builder is not None and isinstance(delta, str):
        builder.add_delta(delta)
    return ()


def _on_arguments_done(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    builder = state.tools.for_event(event)
    arguments = event.get("arguments")
    if builder is not None and isinstance(arguments, str):
        builder.set_arguments(arguments)
    return ()


def _on_output_text(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    delta = event.get("delta")
    if not (isinstance(delta, str) and delta):
        return ()
    state.content_parts.append(delta)
    return (ProviderTextDeltaEvent(delta=delta),)


def _on_reasoning_text(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    delta = event.get("delta")
    if not (isinstance(delta, str) and delta):
        return ()
    state.thinking_parts.append(delta)
    return (ProviderThinkingDeltaEvent(delta=delta),)


def _on_reasoning_part_done(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    """Reasoning arrives in parts that the API concatenates, so separate them here."""
    if not state.thinking_parts:
        return ()
    separator = "\n\n"
    state.thinking_parts.append(separator)
    return (ProviderThinkingDeltaEvent(delta=separator),)


def _on_item_done(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    item = _event_item(event)
    if item is None:
        return ()
    kind = item.get("type")
    if kind == "reasoning":
        state.remember_reasoning(item)
        return ()
    if kind == "function_call":
        return _complete_tool_call(state, event, item)
    if kind == "message" and not state.content_parts:
        text = _text_from_done_message(item)
        if text:
            state.content_parts.append(text)
            return (ProviderTextDeltaEvent(delta=text),)
    return ()


def _complete_tool_call(
    state: _CodexStreamState, event: dict[str, JSONValue], item: Mapping[str, Any]
) -> tuple[ProviderEvent, ...]:
    """Build a finished tool call, tracking it first when it was never announced."""
    builder = state.tools.for_event(event)
    if builder is None:
        builder = _tool_builder_from_item(item)
        state.tools.track(builder, event)
    else:
        builder.update_from_item(item)
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        builder.set_arguments(arguments)
    tool_call = builder.build()
    state.tool_calls.append(tool_call)
    state.tools.untrack(builder)
    return (ProviderToolCallEvent(tool_call=tool_call),)


def _on_response_ended(
    state: _CodexStreamState, event: dict[str, JSONValue]
) -> tuple[ProviderEvent, ...]:
    """The terminal event carries the finish reason and, usually, the usage."""
    state.finish_reason = _finish_reason_from_response(event)
    state.usage = _usage_from_response(event) or state.usage
    state.ended = True
    return ()


_CODEX_HANDLERS: dict[str, _CodexHandler] = {
    "error": _on_codex_error,
    "response.failed": _on_codex_failed,
    "response.output_item.added": _on_item_added,
    "response.function_call_arguments.delta": _on_arguments_delta,
    "response.function_call_arguments.done": _on_arguments_done,
    "response.output_text.delta": _on_output_text,
    "response.reasoning.delta": _on_reasoning_text,
    "response.reasoning_summary_text.delta": _on_reasoning_text,
    "response.reasoning_text.delta": _on_reasoning_text,
    "response.reasoning_summary_part.done": _on_reasoning_part_done,
    "response.output_item.done": _on_item_done,
    "response.output_item.completed": _on_item_done,
    "response.done": _on_response_ended,
    "response.completed": _on_response_ended,
    "response.incomplete": _on_response_ended,
}


async def _iter_sse_objects(response: httpx.Response) -> AsyncIterator[dict[str, JSONValue]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped:
            if data_lines:
                data = "\n".join(data_lines).strip()
                data_lines = []
                parsed = _loads_object(data)
                if parsed is not None:
                    yield parsed
            continue
        if not stripped.startswith("data:"):
            continue
        value = stripped.removeprefix("data:").strip()
        if value == "[DONE]":
            break
        data_lines.append(value)

    if data_lines:
        parsed = _loads_object("\n".join(data_lines).strip())
        if parsed is not None:
            yield parsed


def _tool_builder_from_item(item: Mapping[str, Any]) -> _ToolCallBuilder:
    call_id = item.get("call_id")
    name = item.get("name")
    item_id = item.get("id")
    return _ToolCallBuilder(
        call_id=call_id if isinstance(call_id, str) and call_id else "call_0",
        item_id=item_id if isinstance(item_id, str) and item_id else None,
        name=name if isinstance(name, str) else "",
    )


def _track_tool_builder(
    builder: _ToolCallBuilder,
    event: Mapping[str, Any],
    *,
    active_tools: list[_ToolCallBuilder],
    by_item_id: dict[str, _ToolCallBuilder],
    by_call_id: dict[str, _ToolCallBuilder],
    by_output_index: dict[int, _ToolCallBuilder],
) -> None:
    if builder not in active_tools:
        active_tools.append(builder)
    if builder.item_id:
        by_item_id[builder.item_id] = builder
    if builder.call_id:
        by_call_id[builder.call_id] = builder
    output_index = _event_output_index(event)
    if output_index is not None:
        by_output_index[output_index] = builder


def _untrack_tool_builder(
    builder: _ToolCallBuilder,
    *,
    active_tools: list[_ToolCallBuilder],
    by_item_id: dict[str, _ToolCallBuilder],
    by_call_id: dict[str, _ToolCallBuilder],
    by_output_index: dict[int, _ToolCallBuilder],
) -> None:
    if builder in active_tools:
        active_tools.remove(builder)
    if builder.item_id and by_item_id.get(builder.item_id) is builder:
        del by_item_id[builder.item_id]
    if builder.call_id and by_call_id.get(builder.call_id) is builder:
        del by_call_id[builder.call_id]
    for output_index, tracked_builder in tuple(by_output_index.items()):
        if tracked_builder is builder:
            del by_output_index[output_index]


def _tool_builder_for_event(
    event: Mapping[str, Any],
    *,
    active_tools: list[_ToolCallBuilder],
    by_item_id: dict[str, _ToolCallBuilder],
    by_call_id: dict[str, _ToolCallBuilder],
    by_output_index: dict[int, _ToolCallBuilder],
) -> _ToolCallBuilder | None:
    item_id = _event_item_id(event)
    if item_id is not None and item_id in by_item_id:
        return by_item_id[item_id]
    call_id = _event_call_id(event)
    if call_id is not None and call_id in by_call_id:
        return by_call_id[call_id]
    output_index = _event_output_index(event)
    if output_index is not None and output_index in by_output_index:
        return by_output_index[output_index]
    if len(active_tools) == 1:
        return active_tools[0]
    return None


def _event_item_id(event: Mapping[str, Any]) -> str | None:
    item_id = event.get("item_id")
    if isinstance(item_id, str) and item_id:
        return item_id
    item = event.get("item")
    if isinstance(item, Mapping):
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            return item_id
    return None


def _event_call_id(event: Mapping[str, Any]) -> str | None:
    call_id = event.get("call_id")
    if isinstance(call_id, str) and call_id:
        return call_id
    item = event.get("item")
    if isinstance(item, Mapping):
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            return call_id
    return None


def _event_output_index(event: Mapping[str, Any]) -> int | None:
    output_index = event.get("output_index")
    if isinstance(output_index, int) and not isinstance(output_index, bool):
        return output_index
    return None


def _text_from_done_message(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "output_text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif part.get("type") == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str):
                parts.append(refusal)
    return "".join(parts)


def _finish_reason_from_response(event: Mapping[str, Any]) -> str | None:
    response = event.get("response")
    if not isinstance(response, Mapping):
        return None
    status = response.get("status")
    if isinstance(status, str):
        return status
    return None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_from_response(event: Mapping[str, Any]) -> Usage | None:
    """Parse billed usage from a Responses ``response.completed``-style event.

    Cache reads and writes are subtracted from ``input_tokens`` to leave fresh
    input. Cost is left unset (None) because Run Agent has no per-model pricing table.
    """
    response = event.get("response")
    if not isinstance(response, Mapping):
        return None
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_details = raw.get("input_tokens_details")
    cache_read = (
        _int_or_zero(input_details.get("cached_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    cache_write = (
        _int_or_zero(input_details.get("cache_write_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    output_details = raw.get("output_tokens_details")
    # Leave reasoning None (not 0) when the provider reports no breakdown,
    # honoring the "None = not reported" contract on Usage.
    reasoning = (
        _int_or_zero(output_details.get("reasoning_tokens"))
        if isinstance(output_details, Mapping)
        else None
    )
    return Usage(
        input=max(0, _int_or_zero(raw.get("input_tokens")) - cache_read - cache_write),
        output=_int_or_zero(raw.get("output_tokens")),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
        total_tokens=_int_or_zero(raw.get("total_tokens")),
    )


def _response_error_message(event: Mapping[str, Any]) -> str:
    code, message = _stream_error_details(event)
    if message:
        return message
    if code:
        return f"OpenAI Codex response failed: {code}"
    return "OpenAI Codex response failed"


def _error_message(event: Mapping[str, Any], *, fallback: str) -> str:
    code, message = _stream_error_details(event)
    if message:
        return message
    if code:
        return code
    return fallback


def _stream_error_details(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract the machine code and human message from a Codex stream error.

    Codex reports failures either as a top-level ``error`` SSE event with a
    nested ``error`` object (``{"type":"error","error":{"code":...}}``) or
    as ``response.failed`` with the failure under ``response.error``. Looking
    only at top-level fields hides details such as ``server_is_overloaded``
    behind a generic fallback message.
    """
    sources: list[Mapping[str, Any]] = [event]
    nested = event.get("error")
    if isinstance(nested, Mapping):
        sources.append(nested)
    response = event.get("response")
    if isinstance(response, Mapping):
        response_error = response.get("error")
        if isinstance(response_error, Mapping):
            sources.append(response_error)

    message: str | None = None
    code: str | None = None
    for source in sources:
        if message is None:
            raw_message = source.get("message")
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
        if code is None:
            raw_code = source.get("code")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code
    if code is None and isinstance(nested, Mapping):
        nested_type = nested.get("type")
        if isinstance(nested_type, str) and nested_type:
            code = nested_type
    return code, message


_TRANSIENT_STREAM_ERROR_MARKERS = (
    "overloaded",
    "service_unavailable",
    "temporarily_unavailable",
    "rate_limit",
    "internal_error",
    "server_error",
    "timeout",
)


def _stream_error_event_data(event: ProviderErrorEvent) -> dict[str, JSONValue] | None:
    """Return the raw SSE event attached to a provider stream error."""
    data = event.data
    if not isinstance(data, dict):
        return None
    raw = data.get("event")
    return raw if isinstance(raw, dict) else None


def _retryable_stream_error_event(event: ProviderErrorEvent) -> bool:
    """Return True when an in-stream Codex error looks transient and retryable."""
    raw = _stream_error_event_data(event)
    if raw is None:
        return False
    code, message = _stream_error_details(raw)
    haystack = " ".join(part for part in (code, message) if part).lower()
    if not haystack or _is_terminal_rate_limit(haystack):
        return False
    return any(marker in haystack for marker in _TRANSIENT_STREAM_ERROR_MARKERS)


def _build_codex_headers(
    configured_headers: Mapping[str, str] | None,
    *,
    access_token: str,
    account_id: str,
    originator: str,
    session_id: str | None = None,
) -> dict[str, str]:
    headers = {
        **dict(configured_headers or {}),
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "originator": originator,
        "User-Agent": f"run-agent ({system()} {release()}; {machine()})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if session_id is not None:
        headers["session-id"] = session_id
    return headers


def _resolve_codex_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def _resolve_codex_models_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return f"{normalized.removesuffix('/responses')}/models"
    if normalized.endswith("/codex"):
        return f"{normalized}/models"
    return f"{normalized}/codex/models"


def _parse_codex_model_limits(payload: object) -> dict[str, RuntimeModelLimits]:
    if not isinstance(payload, Mapping):
        return {}
    models = payload.get("models")
    if not isinstance(models, list):
        return {}

    parsed: dict[str, RuntimeModelLimits] = {}
    for item in models:
        if not isinstance(item, Mapping):
            continue
        model = item.get("slug")
        context_window = _positive_int(item.get("context_window")) or _positive_int(
            item.get("max_context_window")
        )
        if not isinstance(model, str) or not model or context_window is None:
            continue
        effective_percent = _positive_int(item.get("effective_context_window_percent")) or 100
        if effective_percent > 100:
            continue
        parsed[model] = RuntimeModelLimits(
            context_window=context_window,
            max_output_tokens=_positive_int(item.get("max_output_tokens")),
            effective_context_window_percent=effective_percent,
            auto_compact_token_limit=_positive_int(item.get("auto_compact_token_limit")),
        )
    return parsed


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _split_tool_call_id(value: str) -> tuple[str, str | None]:
    if "|" not in value:
        return value, None
    call_id, item_id = value.split("|", 1)
    return call_id, item_id or None


def _loads_object(value: str) -> dict[str, JSONValue] | None:
    try:
        loaded = loads(value)
    except JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _is_retryable_status(status_code: int, body: str) -> bool:
    if status_code == 429 and _is_terminal_rate_limit(body):
        return False
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_terminal_rate_limit(body: str) -> bool:
    normalized = body.lower()
    markers = (
        "gousagelimiterror",
        "freeusagelimiterror",
        "monthly usage limit reached",
        "available balance",
        "insufficient_quota",
        "out of budget",
        "quota exceeded",
        "billing",
    )
    return any(marker in normalized for marker in markers)
