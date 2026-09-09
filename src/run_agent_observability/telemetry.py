"""Physical provider-call ledger and Agent event tracing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Any, Literal
from uuid import uuid4

from run_agent_ai.http import (
    HttpAttempt,
    ProviderCallContext,
    add_http_attempt_observer,
    provider_call_scope,
)
from run_agent_ai.model_limits import ModelLimitsProvider, RuntimeModelLimits
from run_agent_core.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from run_agent_core.messages import AgentMessage, AssistantMessage
from run_agent_core.provider import CancellationToken, ModelProvider
from run_agent_core.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
)
from run_agent_core.tools import AgentTool
from run_agent_core.types import JSONValue
from run_agent_observability.sink import TelemetrySink, read_stream


class ProviderCallLedger:
    """Correlate physical HTTP attempts with logical provider streams."""

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        stream: str,
        root_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.sink = sink
        self.stream = stream
        self.root_id = root_id or uuid4().hex
        self.session_id = session_id
        self._incomplete = False
        self._attempt_counts: dict[str, int] = {}
        self._active_call_ids: set[str] = set()
        self._lock = Lock()
        self._unsubscribe = add_http_attempt_observer(self._record_attempt)
        self._closed = False

    @property
    def path(self) -> Path:
        return self.sink.path

    def instrument(self, provider: ModelProvider, *, provider_name: str) -> LedgeredProvider:
        if isinstance(provider, LedgeredProvider) and provider._ledger is self:
            return provider
        return LedgeredProvider(provider, provider_name=provider_name, ledger=self)

    @property
    def complete(self) -> bool:
        return not self._incomplete and not self._active_call_ids

    async def read_all(self) -> list[dict[str, Any]]:
        try:
            await self.sink.flush()
        except Exception:
            self._incomplete = True
        # Even after a failed write, already committed evidence remains useful.
        records = await read_stream(self.sink, self.stream, flush=False)
        started = {
            row["logical_call_id"] for row in records if row["type"] == "provider_call_start"
        }
        finished = {row["id"] for row in records if row["type"] == "provider_call"}
        self._incomplete |= bool(started - finished)
        return records

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()

    async def _record_attempt(self, attempt: HttpAttempt) -> None:
        logical_call_id = attempt.logical_call_id
        if logical_call_id is None:
            return
        with self._lock:
            if logical_call_id not in self._active_call_ids:
                return
            self._attempt_counts[logical_call_id] = self._attempt_counts.get(logical_call_id, 0) + 1
        try:
            await self.sink.append(
                self.stream,
                {
                    "type": "http_attempt",
                    "root_id": self.root_id,
                    **asdict(attempt),
                },
            )
        except BaseException:
            self._incomplete = True
            raise

    async def begin_call(self, context: ProviderCallContext) -> None:
        if self._closed:
            raise RuntimeError("Provider ledger is closed")
        with self._lock:
            self._active_call_ids.add(context.logical_call_id)
            self._attempt_counts[context.logical_call_id] = 0
        await self.sink.append(
            self.stream,
            {
                "type": "provider_call_start",
                "root_id": self.root_id,
                **asdict(context),
                "started_at": time(),
            },
        )

    async def record_call(
        self,
        *,
        logical_call_id: str,
        provider: str,
        model: str,
        session_id: str | None,
        started_at: float,
        duration_ms: float,
        status: str,
        message: AssistantMessage | None,
        error: str | None,
    ) -> None:
        usage = message.usage if message is not None else None
        with self._lock:
            physical_attempts = self._attempt_counts.pop(logical_call_id, 0)
            self._active_call_ids.discard(logical_call_id)
        try:
            await self.sink.append(
                self.stream,
                {
                    "type": "provider_call",
                    "root_id": self.root_id,
                    "id": logical_call_id,
                    "provider": provider,
                    "model": model,
                    "session_id": session_id,
                    "started_at": started_at,
                    "duration_ms": round(duration_ms, 3),
                    "status": status,
                    "error": error,
                    "physical_attempts": physical_attempts,
                    "retry_count": max(0, physical_attempts - 1),
                    "input_tokens": usage.input if usage is not None else 0,
                    "output_tokens": usage.output if usage is not None else 0,
                    "cache_read_tokens": usage.cache_read if usage is not None else 0,
                    "cache_write_tokens": usage.cache_write if usage is not None else 0,
                    "cache_write_1h_tokens": (
                        usage.cache_write_1h if usage is not None and usage.cache_write_1h else 0
                    ),
                    "cost": usage.cost.total if usage is not None else 0.0,
                    "usage_observed": message is not None
                    and (status == "succeeded" or usage is not None and usage.total_tokens > 0),
                },
            )
        except Exception:
            # Failure evidence must survive an optional reporting failure. The
            # host reports incomplete accounting and never a zero-cost success.
            self._incomplete = True


class LedgeredProvider:
    """ModelProvider decorator that opens a correlation scope per stream."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        provider_name: str,
        ledger: ProviderCallLedger,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._ledger = ledger
        self._closed = False

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
        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            scoped_session_id = session_id or self._ledger.session_id
            logical_call_id = uuid4().hex
            started_at = time()
            started_monotonic = monotonic()
            final: AssistantMessage | None = None
            status = "error"
            error: str | None = None
            context = ProviderCallContext(
                logical_call_id=logical_call_id,
                provider=self._provider_name,
                model=model,
                session_id=scoped_session_id,
            )
            try:
                await self._ledger.begin_call(context)
                with provider_call_scope(context):
                    events = self._provider.stream_response(
                        model=model,
                        system=system,
                        messages=messages,
                        tools=tools,
                        signal=signal,
                        session_id=scoped_session_id,
                    )
                    try:
                        async for event in events:
                            if isinstance(event, AssistantDoneEvent):
                                final = event.message
                                status = "succeeded"
                            elif isinstance(event, AssistantErrorEvent):
                                final = event.error
                                error = event.error.error_message
                                status = event.error.stop_reason
                            else:
                                final = event.partial
                            yield event
                    finally:
                        close = getattr(events, "aclose", None)
                        if close is not None:
                            await close()
            except asyncio.CancelledError:
                status = "cancelled"
                error = "provider stream cancelled"
                raise
            except Exception as exc:
                status = "error"
                error = str(exc) or type(exc).__name__
                raise
            except GeneratorExit:
                if status != "succeeded":
                    status, error = "cancelled", "provider stream closed before completion"
                raise
            finally:
                await self._ledger.record_call(
                    logical_call_id=logical_call_id,
                    provider=self._provider_name,
                    model=model,
                    session_id=scoped_session_id,
                    started_at=started_at,
                    duration_ms=(monotonic() - started_monotonic) * 1000,
                    status=status,
                    message=final,
                    error=error,
                )

        return iterator()

    async def discover_model_limits(self, model: str) -> RuntimeModelLimits | None:
        if isinstance(self._provider, ModelLimitsProvider):
            return await self._provider.discover_model_limits(model)
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()


@dataclass(frozen=True, slots=True)
class TraceSpan:
    id: str
    trace_id: str
    session_id: str | None
    name: str
    started_at: float
    duration_ms: float
    status: Literal["ok", "error", "cancelled"] = "ok"
    attributes: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCallSummary:
    logical_calls: int
    successful_calls: int
    failed_calls: int
    physical_attempts: int
    retry_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    total_cost: float

    @property
    def attempts_per_logical_call(self) -> float:
        return self.physical_attempts / self.logical_calls if self.logical_calls else 0.0

    @property
    def cost_per_successful_call(self) -> float | None:
        if not self.successful_calls:
            return None
        return self.total_cost / self.successful_calls


class TraceRecorder:
    """Agent event listener that writes one span per turn, provider, and tool."""

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        session_id: str | None = None,
        stream: str,
    ) -> None:
        self.sink = sink
        self.stream = stream
        self.span_count = 0
        self.dropped_count = 0
        self.session_id = session_id
        self._trace_id: str | None = None
        self._agent_started: float | None = None
        self._turn_started: float | None = None
        self._tool_started: dict[str, float] = {}

    @property
    def path(self) -> Path:
        return self.sink.path

    async def __call__(self, event: AgentEvent | object) -> None:
        now = monotonic()
        if isinstance(event, AgentStartEvent):
            self._trace_id = uuid4().hex
            self._agent_started = now
            return
        if getattr(event, "type", None) == "turn_start":
            self._turn_started = now
            return
        if isinstance(event, ToolExecutionStartEvent):
            self._tool_started[event.tool_call_id] = now
            return
        if isinstance(event, ToolExecutionEndEvent):
            started = self._tool_started.pop(event.tool_call_id, now)
            self._append_span(
                name=f"tool:{event.tool_name}",
                started=started,
                finished=now,
                status="error" if event.is_error else "ok",
                attributes={"tool_call_id": event.tool_call_id},
            )
            return
        if isinstance(event, MessageEndEvent) and isinstance(
            event.message,
            AssistantMessage,
        ):
            timing = event.message.timing
            duration = timing.total_duration_ms if timing is not None else 0
            self._append_span(
                name="provider",
                started=max(0.0, now - duration / 1000),
                finished=now,
                status=(
                    "error"
                    if event.message.stop_reason == "error"
                    else "cancelled"
                    if event.message.stop_reason == "aborted"
                    else "ok"
                ),
                attributes={
                    "model": event.message.model,
                    "provider": event.message.provider,
                    "input_tokens": event.message.usage.input,
                    "output_tokens": event.message.usage.output,
                    "cost": event.message.usage.cost.total,
                },
            )
            return
        if getattr(event, "type", None) == "turn_end":
            started = self._turn_started if self._turn_started is not None else now
            message = getattr(event, "message", None)
            tool_results = getattr(event, "tool_results", ())
            turn_failed = isinstance(message, AssistantMessage) and message.stop_reason == "error"
            self._append_span(
                name="turn",
                started=started,
                finished=now,
                status="error" if turn_failed else "ok",
                attributes={"tool_result_count": len(tool_results)},
            )
            self._turn_started = None
            return
        if isinstance(event, AgentEndEvent):
            started = self._agent_started if self._agent_started is not None else now
            self._append_span(
                name="agent",
                started=started,
                finished=now,
                attributes={"new_message_count": len(event.messages)},
            )
            self._agent_started = None

    async def read_all(self) -> list[dict[str, Any]]:
        return await read_stream(self.sink, self.stream)

    def _append_span(
        self,
        *,
        name: str,
        started: float,
        finished: float,
        status: Literal["ok", "error", "cancelled"] = "ok",
        attributes: dict[str, JSONValue] | None = None,
    ) -> None:
        trace_id = self._trace_id or uuid4().hex
        span = TraceSpan(
            id=uuid4().hex,
            trace_id=trace_id,
            session_id=self.session_id,
            name=name,
            started_at=started,
            duration_ms=round(max(0.0, finished - started) * 1000, 3),
            status=status,
            attributes=attributes or {},
        )
        if self.sink.emit(self.stream, {"type": "span", **asdict(span)}):
            self.span_count += 1
        else:
            self.dropped_count += 1


def percentile(values: Sequence[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile_value)
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarize_spans(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[float]] = {}
    for record in records:
        if record.get("type") != "span":
            continue
        name = str(record.get("name", "unknown"))
        groups.setdefault(name, []).append(float(record.get("duration_ms", 0)))
    return {
        name: {
            "count": len(values),
            "p50_ms": percentile(values, 0.50),
            "p95_ms": percentile(values, 0.95),
            "max_ms": max(values),
        }
        for name, values in groups.items()
    }


def summarize_provider_calls(records: Sequence[Mapping[str, Any]]) -> ProviderCallSummary:
    """Reduce logical provider-call records without mixing in HTTP-attempt rows."""
    calls = [record for record in records if record.get("type") == "provider_call"]
    successful = sum(record.get("status") == "succeeded" for record in calls)
    logical_ids = {
        row["logical_call_id"] for row in records if row.get("type") == "provider_call_start"
    }
    logical_ids.update(row["id"] for row in calls)
    attempts: dict[str, int] = {}
    for row in records:
        if row.get("type") == "http_attempt":
            identity = str(row["logical_call_id"])
            attempts[identity] = attempts.get(identity, 0) + 1
    for row in calls:
        identity = str(row["id"])
        attempts[identity] = max(attempts.get(identity, 0), int(row.get("physical_attempts", 0)))
    return ProviderCallSummary(
        logical_calls=len(logical_ids),
        successful_calls=successful,
        failed_calls=len(logical_ids) - successful,
        physical_attempts=sum(attempts.values()),
        retry_count=sum(max(0, count - 1) for count in attempts.values()),
        input_tokens=sum(max(0, int(record.get("input_tokens", 0))) for record in calls),
        output_tokens=sum(max(0, int(record.get("output_tokens", 0))) for record in calls),
        cache_read_tokens=sum(max(0, int(record.get("cache_read_tokens", 0))) for record in calls),
        cache_write_tokens=sum(
            max(0, int(record.get("cache_write_tokens", 0))) for record in calls
        ),
        cache_write_1h_tokens=sum(
            max(0, int(record.get("cache_write_1h_tokens", 0))) for record in calls
        ),
        total_cost=sum(max(0.0, float(record.get("cost", 0.0))) for record in calls),
    )


__all__ = [
    "LedgeredProvider",
    "ProviderCallLedger",
    "ProviderCallSummary",
    "TraceRecorder",
    "TraceSpan",
    "percentile",
    "summarize_spans",
    "summarize_provider_calls",
]
