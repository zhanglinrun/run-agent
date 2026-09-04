"""Physical provider-call ledger and Agent event tracing."""

from __future__ import annotations

import asyncio
import json
import os
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


class JsonlRecorder:
    """Thread-safe append-only JSONL sink with process-local sequencing."""

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self._lock = Lock()
        self._seq = self._existing_count()

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, **payload}
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                file.flush()
                if self._fsync:
                    os.fsync(file.fileno())
            return record

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        for expected, record in enumerate(records, start=1):
            if record.get("seq") != expected:
                raise ValueError(
                    f"JSONL sequence mismatch at line {expected}: got {record.get('seq')}"
                )
        return records

    def _existing_count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text(encoding="utf-8").splitlines() if line)


class ProviderCallLedger:
    """Correlate physical HTTP attempts with logical provider streams."""

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self._recorder = JsonlRecorder(path, fsync=fsync)
        self._attempt_counts: dict[str, int] = {}
        self._active_call_ids: set[str] = set()
        self._lock = Lock()
        self._unsubscribe = add_http_attempt_observer(self._record_attempt)
        self._closed = False

    @property
    def path(self) -> Path:
        return self._recorder.path

    def instrument(self, provider: ModelProvider, *, provider_name: str) -> LedgeredProvider:
        return LedgeredProvider(provider, provider_name=provider_name, ledger=self)

    def read_all(self) -> list[dict[str, Any]]:
        return self._recorder.read_all()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()

    def _record_attempt(self, attempt: HttpAttempt) -> None:
        logical_call_id = attempt.logical_call_id
        if logical_call_id is None:
            return
        with self._lock:
            if logical_call_id not in self._active_call_ids:
                return
            self._attempt_counts[logical_call_id] = self._attempt_counts.get(logical_call_id, 0) + 1
        self._recorder.append(
            {
                "type": "http_attempt",
                **asdict(attempt),
            }
        )

    def begin_call(self, logical_call_id: str) -> None:
        with self._lock:
            self._active_call_ids.add(logical_call_id)
            self._attempt_counts[logical_call_id] = 0

    def record_call(
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
        self._recorder.append(
            {
                "type": "provider_call",
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
            }
        )


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
                session_id=session_id,
            )
            self._ledger.begin_call(logical_call_id)
            try:
                with provider_call_scope(context):
                    async for event in self._provider.stream_response(
                        model=model,
                        system=system,
                        messages=messages,
                        tools=tools,
                        signal=signal,
                        session_id=session_id,
                    ):
                        if isinstance(event, AssistantDoneEvent):
                            final = event.message
                            status = "succeeded"
                        elif isinstance(event, AssistantErrorEvent):
                            final = event.error
                            error = event.error.error_message
                            status = event.error.stop_reason
                        yield event
            except asyncio.CancelledError:
                status = "cancelled"
                error = "provider stream cancelled"
                raise
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                raise
            finally:
                self._ledger.record_call(
                    logical_call_id=logical_call_id,
                    provider=self._provider_name,
                    model=model,
                    session_id=session_id,
                    started_at=started_at,
                    duration_ms=(monotonic() - started_monotonic) * 1000,
                    status=status,
                    message=final,
                    error=error,
                )

        return iterator()

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
        path: str | Path,
        *,
        session_id: str | None = None,
        fsync: bool = True,
    ) -> None:
        self._recorder = JsonlRecorder(path, fsync=fsync)
        self.session_id = session_id
        self._trace_id: str | None = None
        self._agent_started: float | None = None
        self._turn_started: float | None = None
        self._tool_started: dict[str, float] = {}

    @property
    def path(self) -> Path:
        return self._recorder.path

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

    def read_all(self) -> list[dict[str, Any]]:
        return self._recorder.read_all()

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
        self._recorder.append({"type": "span", **asdict(span)})


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
    return ProviderCallSummary(
        logical_calls=len(calls),
        successful_calls=successful,
        failed_calls=len(calls) - successful,
        physical_attempts=sum(max(0, int(record.get("physical_attempts", 0))) for record in calls),
        retry_count=sum(max(0, int(record.get("retry_count", 0))) for record in calls),
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
    "JsonlRecorder",
    "LedgeredProvider",
    "ProviderCallLedger",
    "ProviderCallSummary",
    "TraceRecorder",
    "TraceSpan",
    "percentile",
    "summarize_spans",
    "summarize_provider_calls",
]
