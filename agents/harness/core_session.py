"""One task session assembled from AgentCore and an ExtensionHost."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..extensions import ExtensionContext, ExtensionHost, ExtensionToolExecutor
from ..providers import AnthropicProviderAdapter, OpenAIProviderAdapter
from ..runtime import AgentCore, CompositeAgentHooks, EventBus
from ..runtime.contracts import EventType, ToolCall
from ..runtime.tracing import TraceRecorder
from ..session import SessionReducer
from .middleware import BudgetMiddleware, SessionTaskMiddleware
from .task import TaskState, TaskSpec


class CoreSession:
    """Bind the provider-neutral loop to one already-loaded extension host."""

    def __init__(
        self,
        *,
        task: TaskSpec,
        state: TaskState,
        repository: Any,
        journal: Any,
        provider: Any,
        host: ExtensionHost,
        context: ExtensionContext,
        trace: TraceRecorder,
    ) -> None:
        self.task = task
        self.state = state
        self.repository = repository
        self.journal = journal
        self.provider = provider
        self.host = host
        self.extension_context = context
        self.trace = trace
        self.session_middleware = SessionTaskMiddleware(
            state, repository, journal, trace=trace
        )
        events = EventBus()
        events.subscribe(self._record_core_event)
        definitions = host.tool_definitions(context)
        self.core = AgentCore(
            provider=provider,
            tool_executor=ExtensionToolExecutor(host),
            system_prompt=context.base_prompt,
            tools=definitions,
            hooks=CompositeAgentHooks(
                [self.session_middleware, host, BudgetMiddleware(state)]
            ),
            events=events,
            max_turns=task.budget.total_turns,
        )
        session = task.runtime.session
        resume_messages = session.resume_messages
        if resume_messages is None and session.resume_session_id:
            resume_messages = tuple(
                SessionReducer(repository).messages(
                    state.session_id, lane_id=state.lane_id
                )
            )
        if resume_messages is not None:
            replayed = [dict(item) for item in resume_messages if isinstance(item, dict)]
            self.core.context.messages.extend(replayed)
            self.session_middleware.remember_messages(replayed)

    async def _record_core_event(self, name: str, payload: dict[str, Any]) -> None:
        if name == "model_request":
            messages = payload.get("messages") or ()
            digest = hashlib.sha256(
                json.dumps(
                    messages,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            self.trace.emit(
                EventType.MODEL_REQUEST,
                turn=payload.get("turn"),
                context_digest=digest,
                message_count=len(messages),
                tool_names=list(payload.get("tool_names") or ()),
            )
        elif name == "model_response":
            self.trace.emit(
                EventType.MODEL_RESPONSE,
                turn=payload.get("turn"),
                text=payload.get("text") or "",
                tool_count=len(payload.get("tool_calls") or ()),
                stop_reason=payload.get("stop_reason") or "",
                usage=payload.get("usage") or {},
            )

        elif name == "tool_effective":
            call = payload.get("call")
            if isinstance(call, ToolCall):
                self.trace.emit(
                    EventType.TOOL_EFFECTIVE,
                    turn=payload.get("turn"),
                    call_id=call.id,
                    name=call.name,
                    input=call.input,
                )

    async def run_once(self, prompt: str, *, max_turns: int) -> dict[str, Any]:
        with self.host.use_context(self.extension_context):
            result = await self.core.run(prompt, max_turns=max_turns)
        self.session_middleware.flush_context(self.core.context)
        return {
            "text": result.text,
            "tokens": result.usage,
            "turns": result.turns,
            "turns_accounted": True,
            "usage_accounted": True,
        }


def build_provider(task: TaskSpec) -> Any:
    settings = task.runtime.provider
    if settings.adapter is not None:
        return settings.adapter
    if not settings.api_key:
        raise RuntimeError("TaskSpec.runtime.provider.api_key is required")
    if settings.use_openai:
        return OpenAIProviderAdapter(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=settings.temperature,
            thinking=settings.thinking,
        )
    return AnthropicProviderAdapter(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.api_base,
        temperature=settings.temperature,
        thinking=settings.thinking,
    )


__all__ = ["CoreSession", "build_provider"]
