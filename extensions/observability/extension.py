"""Optional append-only Agent span recorder extension."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from uuid import uuid4

from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
)
from run_agent_observability import TraceRecorder, summarize_spans


def setup(api: ExtensionAPI) -> None:
    """Record Agent events as correlated, append-only per-session spans."""
    state: dict[str, TraceRecorder | Path | None] = {"recorder": None, "path": None}

    def prepare_recorder(
        extension_context: ExtensionContext,
        *,
        reset: bool = False,
    ) -> TraceRecorder:
        current = state["recorder"]
        if isinstance(current, TraceRecorder) and not reset:
            return current
        session_id = extension_context.session_id
        trace_key = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id or uuid4().hex).strip("-")
        trace_path = extension_context.paths.traces_dir / f"{trace_key}.jsonl"
        recorder = TraceRecorder(trace_path, session_id=session_id)
        state["recorder"] = recorder
        state["path"] = trace_path
        return recorder

    def session_start(event: object, extension_context: ExtensionContext) -> None:
        del event
        prepare_recorder(extension_context, reset=True)

    async def record(event: object, extension_context: ExtensionContext) -> None:
        recorder = prepare_recorder(extension_context)
        await recorder(event)

    def command(args: str, command_context: ExtensionCommandContext) -> str:
        del args
        recorder = prepare_recorder(command_context.api.context)
        trace_path = state["path"]
        assert isinstance(trace_path, Path)
        summary = summarize_spans(recorder.read_all())
        return f"Trace: {trace_path}\n{json.dumps(summary, ensure_ascii=False, sort_keys=True)}"

    api.on("session_start", cast(ExtensionHandler, session_start))
    api.on("agent_event", cast(ExtensionHandler, record))
    api.register_command(
        "trace",
        command,
        description="Show this session's trace artifact and span summary.",
        usage="/trace",
    )
