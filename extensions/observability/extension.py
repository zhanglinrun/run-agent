"""Optional event tracing through the owning host's SQLite observation sink."""

from __future__ import annotations

import json
from typing import cast

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
)
from run_agent_coding.host.contracts import TaskContext, TaskSpec
from run_agent_core.types import JSONValue
from run_agent_observability import TraceRecorder
from run_agent_observability.telemetry import summarize_spans


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

    async def export(payload: JSONValue, context: TaskContext) -> JSONValue:
        if not state:
            raise RuntimeError("Trace recorder has not started")
        rows = await state[0].read_all()
        report = {
            "session_id": state[0].session_id,
            "spans": rows,
            "summary": summarize_spans(rows),
        }
        artifact = await context.services.scope().artifacts.put(
            json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        return {"digest": artifact.digest, "size": artifact.size, "spans": len(rows)}

    async def command(args: str, context: ExtensionCommandContext) -> str:
        recorder = prepare(context.api.context)
        services = context.api.context.services
        if args.strip() == "export":
            task_id = await services.tasks.submit(TaskSpec("export-trace", {}))
            return f"Trace export queued: {task_id}. Check with /trace status {task_id}."
        if args.startswith("status "):
            task = await services.tasks.status(args[7:].strip())
            return f"{task.task_id}: {task.status}\n{task.result or task.error or ''}"
        return (
            f"Trace database: {recorder.path}\nSession: {recorder.session_id}\n"
            f"Recorded spans: {recorder.span_count}; dropped spans: {recorder.dropped_count}"
        )

    api.on("session_start", cast(ExtensionHandler, started))
    api.on("agent_event", cast(ExtensionHandler, record))
    api.register_task_handler("export-trace", export)
    api.register_command(
        "trace",
        command,
        description="Show or export recorded spans.",
        usage="/trace [export | status task_id]",
    )
