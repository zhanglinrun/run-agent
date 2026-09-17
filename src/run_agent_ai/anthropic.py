"""Anthropic Messages API provider."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from json import loads
from typing import Any, cast

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
    CACHE_RETENTION_LONG,
    CACHE_RETENTION_NONE,
    CACHE_RETENTION_SHORT,
    AnthropicConfig,
    CacheRetention,
)
from run_agent_ai.events import AssistantMessageEvent
from run_agent_ai.http import create_async_client
from run_agent_ai.http_errors import provider_http_error_message
from run_agent_ai.provider import CancellationToken
from run_agent_ai.retry import provider_retry_event, retry_delay_seconds, wait_for_retry
from run_agent_ai.stream import canonicalize_provider_stream
from run_agent_ai.tool_call_ids import portable_tool_call_id
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
    message_to_user,
)
from run_agent_core.provider import run_after_provider_response, run_before_provider_headers
from run_agent_core.tools import AgentTool, ToolCall
from run_agent_core.types import JSONValue

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
CACHE_TTL_LONG = "1h"

# Anthropic rejects a request carrying more than four cache breakpoints, so the
# budget is spent where it buys the most: the tool schemas, the system prompt, and
# the two most recent request tails. See _apply_message_cache_breakpoints.
MAX_CACHE_BREAKPOINTS = 4
SYSTEM_CACHE_BREAKPOINTS = 1
TOOLS_CACHE_BREAKPOINTS = 1
MESSAGE_CACHE_BREAKPOINTS = (
    MAX_CACHE_BREAKPOINTS - SYSTEM_CACHE_BREAKPOINTS - TOOLS_CACHE_BREAKPOINTS
)

# Block types that may carry cache_control.
CACHEABLE_BLOCK_TYPES = frozenset({"text", "image", "tool_result"})


class AnthropicProvider:
    """Provider adapter for Anthropic's streaming Messages API."""

    def __init__(
        self,
        config: AnthropicConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

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
        del session_id
        raw = self._stream_provider_events(
            model=model, system=system, messages=messages, tools=tools, signal=signal
        )
        return canonicalize_provider_stream(
            raw, api="anthropic-messages", provider="anthropic", model=model
        )

    async def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one Anthropic response as provider-neutral events.

        This used to be a 260-line outer function whose entire body was an inner
        ``iterator()`` and a ``return iterator()``: it existed only to say "here is a
        generator", while the request setup, the retry loop and the SSE dispatch sat nine
        levels deep inside it. Those are three separate concerns and each now has a name.
        """
        request = await self._prepare_request(
            model=model, system=system, messages=messages, tools=tools
        )
        async for event in self._stream_attempts(request, model, signal):
            yield event

    async def _prepare_request(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
    ) -> _PreparedRequest:
        """Build the request every attempt will send."""
        payload = _build_messages_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=self._config.max_tokens,
            thinking_budget_tokens=self._config.thinking_budget_tokens,
            thinking_effort=self._config.thinking_effort,
            thinking_mode=self._config.thinking_mode,
            supports_images=self._config.supports_images,
            cache_retention=self._config.cache_retention,
            cache_control_on_tools=self._config.cache_control_on_tools,
        )
        return _PreparedRequest(
            client=self._get_client(),
            url=f"{self._config.base_url.rstrip('/')}/messages",
            headers=self._request_headers(),
            payload=payload,
        )

    def _request_headers(self) -> dict[str, str]:
        """Version header, then configured headers, then the key last so it wins."""
        return {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            **(dict(self._config.headers or {})),
            "x-api-key": self._config.api_key,
        }

    async def _stream_attempts(
        self,
        request: _PreparedRequest,
        model: str,
        signal: CancellationToken | None,
    ) -> AsyncIterator[ProviderEvent]:
        """Retry one prepared request until it completes, fails, or is cancelled."""
        await run_before_provider_headers(request.headers)
        attempt = 0
        while True:
            state = _StreamState(attempt=attempt)
            async for event in self._attempt(request, state, model, signal):
                yield event
            outcome = state.result
            if isinstance(outcome, _Retry):
                yield outcome.event
                attempt += 1
                if not await wait_for_retry(outcome.delay, signal=signal):
                    return
                continue
            if outcome is _StreamOutcome.ABORTED:
                return
            if outcome is _StreamOutcome.COMPLETED:
                for event in state.finish():
                    yield event
                return
            yield outcome
            return

    async def _attempt(
        self,
        request: _PreparedRequest,
        state: _StreamState,
        model: str,
        signal: CancellationToken | None,
    ) -> AsyncIterator[ProviderEvent]:
        """One HTTP attempt: emit whatever it streams, then record why it stopped."""
        try:
            async with request.client.stream(
                "POST", request.url, json=request.payload, headers=request.headers
            ) as response:
                await run_after_provider_response(response.status_code, dict(response.headers))
                if response.status_code >= 400:
                    state.result = await self._http_error_outcome(response, model, state.attempt)
                    return
                async for event in self._iter_stream(response, state, model, signal):
                    yield event
                state.result = self._attempt_outcome(state)
        except httpx.HTTPError as exc:
            state.result = self._network_error_outcome(exc, state.emitted, state.attempt)

    async def _iter_stream(
        self,
        response: httpx.Response,
        state: _StreamState,
        model: str,
        signal: CancellationToken | None,
    ) -> AsyncIterator[ProviderEvent]:
        """Read the SSE body, mutating ``state`` and yielding what each chunk produces."""
        yield ProviderResponseStartEvent(model=model)
        async for line in response.aiter_lines():
            if signal is not None and signal.is_cancelled():
                state.result = _StreamOutcome.ABORTED
                return
            event = _parse_sse_line(line)
            if event is None:
                continue
            chunk = _loads_object(event)
            if chunk is None:
                yield ProviderErrorEvent(message="Provider returned invalid JSON chunk")
                state.result = _StreamOutcome.ABORTED
                return
            for produced in _dispatch(self, chunk, state):
                yield produced
            if state.stream_error is not None:
                return

    def _attempt_outcome(self, state: _StreamState) -> _AttemptResult:
        """Why a completed read stopped: cancelled, retryable, or genuinely finished."""
        if state.aborted:
            return _StreamOutcome.ABORTED
        if state.stream_error is None:
            return _StreamOutcome.COMPLETED
        error_type, _ = _anthropic_stream_error_details(state.stream_error)
        return self._retry(
            f"stream error ({error_type or 'unknown'})",
            state.attempt,
            {"event": state.stream_error},
        )

    async def _http_error_outcome(
        self, response: httpx.Response, model: str, attempt: int
    ) -> _AttemptResult:
        """Decide what an erroring response means: back off, or report it and stop."""
        body_text = (await response.aread()).decode(errors="replace")
        if self._should_retry(attempt, status_code=response.status_code):
            return self._retry(
                f"HTTP {response.status_code}",
                attempt,
                {"status_code": response.status_code, "body": body_text},
            )
        return ProviderErrorEvent(
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

    def _network_error_outcome(
        self, exc: httpx.HTTPError, emitted: bool, attempt: int
    ) -> _AttemptResult:
        """A transport failure is retryable only before any content has been emitted."""
        if not emitted and self._should_retry(attempt):
            return self._retry(
                "network error",
                attempt,
                {"error": str(exc), "error_type": type(exc).__name__},
            )
        return ProviderErrorEvent(message=str(exc), data={"attempts": attempt + 1})

    def _retry(self, reason: str, attempt: int, data: dict[str, JSONValue]) -> _Retry:
        """Build the back-off notice and the delay it asks the caller to wait."""
        delay = retry_delay_seconds(attempt, max_delay_seconds=self._config.max_retry_delay_seconds)
        return _Retry(
            event=provider_retry_event(
                attempt=attempt,
                max_retries=self._config.max_retries,
                delay_seconds=delay,
                reason=reason,
                data=data,
            ),
            delay=delay,
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client

    def _should_retry(self, attempt: int, *, status_code: int | None = None) -> bool:
        if attempt >= self._config.max_retries:
            return False
        return status_code is None or status_code in {408, 409, 425, 429} or status_code >= 500


_TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES = frozenset(
    {
        "api_error",
        "overloaded_error",
        "rate_limit_error",
    }
)


def _anthropic_stream_error_details(event: Mapping[str, JSONValue]) -> tuple[str, str]:
    """Return the provider classification and message from an Anthropic SSE error."""
    error = event.get("error")
    if not isinstance(error, Mapping):
        return "", "Provider returned an error"
    error_type = _string_or_empty(error.get("type"))
    message = _string_or_empty(error.get("message")) or "Provider returned an error"
    return error_type, message


def _retryable_anthropic_stream_error(error_type: str) -> bool:
    """Return whether an Anthropic SSE error is transient and safe to retry."""
    return error_type.lower() in _TRANSIENT_ANTHROPIC_STREAM_ERROR_TYPES


class _AnthropicToolBuilder:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    def build(self, index: int) -> ToolCall:
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(
            id=self.id or f"tool-call-{index}",
            name=self.name,
            arguments=arguments,
        )


def _build_messages_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    max_tokens: int | None = None,
    thinking_budget_tokens: int | None = None,
    thinking_effort: str | None = None,
    thinking_mode: str = "budget",
    supports_images: bool = False,
    cache_retention: CacheRetention = CACHE_RETENTION_SHORT,
    cache_control_on_tools: bool = True,
) -> dict[str, JSONValue]:
    resolved_max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    if thinking_budget_tokens is not None:
        resolved_max_tokens = max(resolved_max_tokens, thinking_budget_tokens + 1024)
    cache_control = _cache_control(cache_retention)
    payload_messages = []
    for message in messages:
        converted = _anthropic_message(message, supports_images=supports_images)
        # Dropping foreign provider reasoning can empty a reasoning-only turn.
        # Anthropic rejects empty assistant content, so omit that inert turn too.
        if converted.get("role") == "assistant" and not converted.get("content"):
            continue
        payload_messages.append(converted)
    _apply_message_cache_breakpoints(payload_messages, cache_control)
    payload: dict[str, JSONValue] = {
        "model": model,
        "max_tokens": resolved_max_tokens,
        "stream": True,
        "system": _anthropic_system(system, cache_control),
        "messages": cast("JSONValue", payload_messages),
    }
    if thinking_mode == "disabled":
        payload["thinking"] = {"type": "disabled"}
    elif thinking_mode == "adaptive" and thinking_effort is not None:
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": thinking_effort}
    elif thinking_budget_tokens is not None:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget_tokens,
        }
    if tools:
        # Some Anthropic-protocol gateways accept cache_control everywhere except
        # inside tool objects, so the tools breakpoint is separately suppressible.
        tools_cache_control = cache_control if cache_control_on_tools else None
        last_index = len(tools) - 1
        payload["tools"] = [
            _anthropic_tool(
                tool, cache_control=tools_cache_control if index == last_index else None
            )
            for index, tool in enumerate(tools)
        ]
    return payload


def _cache_control(cache_retention: CacheRetention) -> dict[str, JSONValue] | None:
    """Return the cache_control marker for a retention preference, if enabled.

    Attach sites copy the result, so no two breakpoints share one dict.
    """
    if cache_retention == CACHE_RETENTION_NONE:
        return None
    if cache_retention == CACHE_RETENTION_LONG:
        return {"type": "ephemeral", "ttl": CACHE_TTL_LONG}
    return {"type": "ephemeral"}


def _anthropic_system(system: str, cache_control: dict[str, JSONValue] | None) -> JSONValue:
    """Build the system field, marking it as a cache breakpoint when enabled."""
    if cache_control is None or not system:
        # An empty text block carrying cache_control is rejected outright.
        return system
    return cast(
        "JSONValue", [{"type": "text", "text": system, "cache_control": dict(cache_control)}]
    )


def _apply_message_cache_breakpoints(
    messages: list[dict[str, JSONValue]],
    cache_control: dict[str, JSONValue] | None,
) -> None:
    """Mark this request's tail and the previous request's tail, in place.

    Two breakpoints rather than one: Anthropic checks at most 20 block positions
    back from a breakpoint when searching for a reusable prefix, and one agent turn
    appends 2N+2 blocks for N tool calls. Marking where the previous request ended
    opens a second lookback window there, so a wide parallel-tool turn still gets
    a read hit instead of falling out of the window. Either position may be
    ineligible, in which case fewer breakpoints are emitted.
    """
    if cache_control is None or not messages:
        return
    indexes = {len(messages) - 1}
    boundary = _previous_request_boundary(messages)
    if boundary is not None:
        indexes.add(boundary)
    if len(indexes) > MESSAGE_CACHE_BREAKPOINTS:  # pragma: no cover - defensive
        raise AssertionError("message cache breakpoints exceed the Anthropic budget")
    for index in indexes:
        _mark_cache_breakpoint(messages[index], cache_control)


def _previous_request_boundary(messages: list[dict[str, JSONValue]]) -> int | None:
    """Return the index where the previous request's message list ended.

    Run Agent's transcript is append-only and every request stops immediately before the
    assistant message it produces, so the last user message preceding the final
    assistant turn is where the previous request's tail breakpoint was placed.

    Two cases return an older position than the literal previous request. A turn
    whose assistant message was empty and errored or aborted is filtered out of
    provider context, and consecutive assistant messages (a retained failure
    followed by a continue) leave no user message at the true boundary. Both only
    shorten the prefix this breakpoint can reuse; a marked position that was never
    written simply opens a lookback window that finds an older entry, and
    breakpoints themselves are not billed.
    """
    last_assistant = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            last_assistant = index
            break
    if last_assistant is None:
        return None
    for index in range(last_assistant - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return None


def _mark_cache_breakpoint(
    message: dict[str, JSONValue],
    cache_control: dict[str, JSONValue],
) -> None:
    """Attach cache_control to a user message's final content block, if eligible."""
    if message.get("role") != "user":
        return
    content = message.get("content")
    if isinstance(content, str):
        if not content:
            return
        message["content"] = [
            {"type": "text", "text": content, "cache_control": dict(cache_control)}
        ]
        return
    if not isinstance(content, list) or not content:
        return
    last_block = content[-1]
    if not isinstance(last_block, dict):
        return
    if last_block.get("type") not in CACHEABLE_BLOCK_TYPES:
        return
    if last_block.get("type") == "tool_result":
        # A breakpoint on a tool_result carrying no content risks the same
        # rejection as an empty text block.
        inner = last_block.get("content")
        if isinstance(inner, list) and not inner:
            return
    last_block["cache_control"] = dict(cache_control)


def _anthropic_message(message: AgentMessage, *, supports_images: bool) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        text, images = text_and_images(
            message.content,
            supports_images=supports_images,
            image_placeholder=NON_VISION_USER_IMAGE_PLACEHOLDER,
        )
        if not images:
            return {"role": "user", "content": text}
        user_content: list[JSONValue] = []
        if text:
            user_content.append({"type": "text", "text": text})
        user_content.extend(_anthropic_image(image) for image in images)
        return {"role": "user", "content": user_content}
    if isinstance(message, AssistantMessage):
        content: list[JSONValue] = []
        for block in message.content:
            if isinstance(block, TextContent):
                content.append({"type": "text", "text": block.text})
            elif isinstance(block, ThinkingContent):
                # Thinking signatures are provider-owned opaque state. Replaying
                # an OpenAI/Google signature as an Anthropic thinking block makes
                # an otherwise portable model switch fail validation.
                if message.api != "anthropic-messages":
                    continue
                thinking: dict[str, JSONValue] = {
                    "type": "thinking",
                    "thinking": block.thinking,
                }
                if block.thinking_signature is not None:
                    thinking["signature"] = block.thinking_signature
                content.append(thinking)
            elif isinstance(block, ToolCall):
                content.append(
                    {
                        "type": "tool_use",
                        "id": portable_tool_call_id(block.id),
                        "name": block.name,
                        "input": block.arguments,
                    }
                )
        return {"role": "assistant", "content": content}
    if isinstance(message, ToolResultMessage):
        text, images = text_and_images(
            message.content,
            supports_images=supports_images,
            image_placeholder=NON_VISION_TOOL_IMAGE_PLACEHOLDER,
        )
        result_content: list[JSONValue] = []
        if text:
            result_content.append({"type": "text", "text": text})
        result_content.extend(_anthropic_image(image) for image in images)
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": portable_tool_call_id(message.tool_call_id),
                    "content": result_content,
                    "is_error": bool(message.is_error),
                }
            ],
        }
    return _anthropic_message(message_to_user(message), supports_images=supports_images)


def _anthropic_image(image: ImageContent) -> dict[str, JSONValue]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.mime_type,
            "data": image.data,
        },
    }


def _anthropic_tool(
    tool: AgentTool,
    *,
    cache_control: dict[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }
    if cache_control is not None:
        payload["cache_control"] = dict(cache_control)
    return payload


def _parse_sse_line(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


def _loads_object(text: str) -> dict[str, Any] | None:
    try:
        value = loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _string_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage_from_message_start(raw: object) -> Usage:
    """Build a Usage from the ``message_start`` event's ``message.usage``.

    Ports Pi's anthropic-messages.ts message_start handling. Cost is left unset
    (None) because Run Agent has no per-model pricing table.
    """
    data = raw if isinstance(raw, Mapping) else {}
    cache_creation = data.get("cache_creation")
    cache_write_1h = (
        _int_or_none(cache_creation.get("ephemeral_1h_input_tokens"))
        if isinstance(cache_creation, Mapping)
        else None
    )
    usage = Usage(
        input=_int_or_none(data.get("input_tokens")) or 0,
        output=_int_or_none(data.get("output_tokens")) or 0,
        cache_read=_int_or_none(data.get("cache_read_input_tokens")) or 0,
        cache_write=_int_or_none(data.get("cache_creation_input_tokens")) or 0,
        cache_write_1h=cache_write_1h,
    )
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage


def _apply_message_delta_usage(usage: Usage | None, raw: object) -> Usage | None:
    """Apply the ``message_delta`` event's ``usage`` onto the running Usage.

    Ports Pi's anthropic-messages.ts message_delta handling: only overwrite
    fields the provider reports (non-null), then recompute the token total.
    """
    if not isinstance(raw, Mapping):
        return usage
    usage = usage or Usage()
    if (value := _int_or_none(raw.get("input_tokens"))) is not None:
        usage.input = value
    if (value := _int_or_none(raw.get("output_tokens"))) is not None:
        usage.output = value
    if (value := _int_or_none(raw.get("cache_read_input_tokens"))) is not None:
        usage.cache_read = value
    if (value := _int_or_none(raw.get("cache_creation_input_tokens"))) is not None:
        usage.cache_write = value
    details = raw.get("output_tokens_details")
    if isinstance(details, Mapping):
        thinking = _int_or_none(details.get("thinking_tokens"))
        if thinking is not None:
            usage.reasoning = thinking
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return usage


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    """Everything one HTTP attempt needs, resolved once per call."""

    client: httpx.AsyncClient
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


class _StreamOutcome(Enum):
    """Why an attempt stopped, when it did not fail."""

    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class _Retry:
    """A failed attempt worth repeating: the notice to emit and how long to wait."""

    event: ProviderEvent
    delay: float


_AttemptResult = ProviderEvent | _Retry | _StreamOutcome


@dataclass(slots=True)
class _StreamState:
    """Everything one attempt accumulates while reading the stream.

    The accumulators used to be nine locals in a 140-line loop body, which is why no
    single event type could be tested without driving a whole stream past it. They are
    fields now, so a handler can be exercised on its own.
    """

    attempt: int = 0
    content_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    thinking_signature: str | None = None
    tool_builders: dict[int, _AnthropicToolBuilder] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: Usage | None = None
    stream_error: dict[str, JSONValue] | None = None
    emitted: bool = False
    aborted: bool = False
    # Defaults to ABORTED so a path that forgets to set it stops rather than reports
    # success; every branch in _attempt assigns it.
    result: _AttemptResult = _StreamOutcome.ABORTED

    def tool_builder(self, index: int) -> _AnthropicToolBuilder:
        """The builder for one content-block index, created on first sight."""
        return self.tool_builders.setdefault(index, _AnthropicToolBuilder())

    def finish(self) -> tuple[ProviderEvent, ...]:
        """The events that close an attempt: its tool calls, then the response."""
        tool_calls = [builder.build(index) for index, builder in sorted(self.tool_builders.items())]
        content = assistant_content("".join(self.content_parts), tool_calls)
        if self.thinking_parts:
            content.insert(
                0,
                ThinkingContent(
                    thinking="".join(self.thinking_parts),
                    thinking_signature=self.thinking_signature,
                ),
            )
        return (
            *(ProviderToolCallEvent(tool_call=call) for call in tool_calls),
            ProviderResponseEndEvent(
                message=AssistantMessage(content=content, usage=self.usage or Usage()),
                finish_reason=self.finish_reason,
            ),
        )


_ChunkHandler = Callable[
    ["AnthropicProvider", dict[str, Any], _StreamState], tuple[ProviderEvent, ...]
]
_DeltaHandler = Callable[
    ["AnthropicProvider", dict[str, Any], Mapping[str, Any], _StreamState],
    tuple[ProviderEvent, ...],
]


def _dispatch(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    """Route one SSE chunk to the handler for its type; unknown types are ignored.

    A table rather than an if/elif chain. The chain was why no single event type could be
    tested on its own, and why recognising a new one meant editing a hundred-line body.
    """
    handler = _HANDLERS.get(chunk.get("type"))
    return () if handler is None else handler(provider, chunk, state)


def _on_message_start(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    message = chunk.get("message")
    if isinstance(message, Mapping):
        state.usage = _usage_from_message_start(message.get("usage"))
    return ()


def _on_content_block_start(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    block = chunk.get("content_block")
    if not (isinstance(block, Mapping) and block.get("type") == "tool_use"):
        return ()
    builder = state.tool_builder(int(chunk.get("index", 0)))
    builder.id = _string_or_empty(block.get("id"))
    builder.name = _string_or_empty(block.get("name"))
    state.emitted = True
    return ()


def _on_content_block_delta(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    delta = chunk.get("delta")
    if not isinstance(delta, Mapping):
        return ()
    handler = _DELTA_HANDLERS.get(delta.get("type"))
    return () if handler is None else handler(provider, chunk, delta, state)


def _on_text_delta(
    provider: AnthropicProvider,
    chunk: dict[str, Any],
    delta: Mapping[str, Any],
    state: _StreamState,
) -> tuple[ProviderEvent, ...]:
    text = _string_or_empty(delta.get("text"))
    if not text:
        return ()
    state.emitted = True
    state.content_parts.append(text)
    return (ProviderTextDeltaEvent(delta=text),)


def _on_thinking_delta(
    provider: AnthropicProvider,
    chunk: dict[str, Any],
    delta: Mapping[str, Any],
    state: _StreamState,
) -> tuple[ProviderEvent, ...]:
    thinking = _string_or_empty(delta.get("thinking"))
    if not thinking:
        return ()
    state.emitted = True
    state.thinking_parts.append(thinking)
    return (ProviderThinkingDeltaEvent(delta=thinking),)


def _on_signature_delta(
    provider: AnthropicProvider,
    chunk: dict[str, Any],
    delta: Mapping[str, Any],
    state: _StreamState,
) -> tuple[ProviderEvent, ...]:
    signature = _string_or_empty(delta.get("signature"))
    if signature:
        state.thinking_signature = f"{state.thinking_signature or ''}{signature}"
    return ()


def _on_input_json_delta(
    provider: AnthropicProvider,
    chunk: dict[str, Any],
    delta: Mapping[str, Any],
    state: _StreamState,
) -> tuple[ProviderEvent, ...]:
    builder = state.tool_builder(int(chunk.get("index", 0)))
    builder.arguments_parts.append(_string_or_empty(delta.get("partial_json")))
    state.emitted = True
    return ()


def _on_message_delta(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    delta = chunk.get("delta")
    if isinstance(delta, Mapping):
        state.finish_reason = _string_or_empty(delta.get("stop_reason")) or state.finish_reason
    state.usage = _apply_message_delta_usage(state.usage, chunk.get("usage"))
    return ()


def _on_error(
    provider: AnthropicProvider, chunk: dict[str, Any], state: _StreamState
) -> tuple[ProviderEvent, ...]:
    """A stream error is retryable only before any content has been emitted."""
    error_type, message = _anthropic_stream_error_details(chunk)
    if (
        not state.emitted
        and provider._should_retry(state.attempt)
        and _retryable_anthropic_stream_error(error_type)
    ):
        state.stream_error = chunk
        return ()
    state.aborted = True
    return (
        ProviderErrorEvent(message=message, data={"event": chunk, "attempts": state.attempt + 1}),
    )


_HANDLERS: dict[Any, _ChunkHandler] = {
    "message_start": _on_message_start,
    "content_block_start": _on_content_block_start,
    "content_block_delta": _on_content_block_delta,
    "message_delta": _on_message_delta,
    "error": _on_error,
}

_DELTA_HANDLERS: dict[Any, _DeltaHandler] = {
    "text_delta": _on_text_delta,
    "thinking_delta": _on_thinking_delta,
    "signature_delta": _on_signature_delta,
    "input_json_delta": _on_input_json_delta,
}
