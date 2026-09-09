"""Optional event tracing through the owning host's SQLite observation sink."""

from __future__ import annotations

from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
)
from run_agent_observability import TraceRecorder


def setup(api: ExtensionAPI) -> None:
    state: list[TraceRecorder] = []

    def prepare(context: ExtensionContext) -> TraceRecorder:
        if not state:
            state.append(
                TraceRecorder(context.telemetry, session_id=context.session_id, stream="trace")
            )
        return state[0]

    def started(event: object, context: ExtensionContext) -> None:
        state.clear()
        prepare(context)

    async def record(event: object, context: ExtensionContext) -> None:
        await prepare(context)(event)

    def command(args: str, context: ExtensionCommandContext) -> str:
        recorder = prepare(context.api.context)
        return (
            f"Trace database: {recorder.path}\nSession: {recorder.session_id}\n"
            f"Recorded spans: {recorder.span_count}; dropped spans: {recorder.dropped_count}"
        )

    api.on("session_start", cast(ExtensionHandler, started))
    api.on("agent_event", cast(ExtensionHandler, record))
    api.register_command(
        "trace", command, description="Show trace location and capture counts.", usage="/trace"
    )
