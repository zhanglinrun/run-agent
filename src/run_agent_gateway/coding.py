"""CodingSession adapter for host-level turn scheduling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable

from run_agent_coding import CodingSession
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage
from run_agent_gateway.models import TurnRequest, TurnResult

SessionResolver = Callable[[str], CodingSession | Awaitable[CodingSession]]


class CodingSessionTurnRunner:
    """Resolve one CodingSession per id and execute its public prompt stream."""

    def __init__(self, resolve_session: SessionResolver) -> None:
        self._resolve_session = resolve_session

    async def run(self, request: TurnRequest, cancellation: asyncio.Event) -> TurnResult:
        if cancellation.is_set():
            return TurnResult.cancelled(request)
        resolved = self._resolve_session(request.session_id)
        session = await resolved if isawaitable(resolved) else resolved
        if request.content.strip() == "/new":
            await session.new_session()
            return TurnResult.succeeded(
                request,
                output="已开始新对话。此前聊天上下文已归档，长期记忆不受影响。",
                metadata={"command": "new"},
            )
        final: AssistantMessage | None = None

        async def consume() -> None:
            nonlocal final
            async for event in session.prompt(request.content):
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message,
                    AssistantMessage,
                ):
                    final = event.message

        consume_task = asyncio.create_task(consume())
        cancel_task = asyncio.create_task(cancellation.wait())
        try:
            done, pending = await asyncio.wait(
                {consume_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancellation.is_set() and not consume_task.done():
                session.cancel()
                await consume_task
            else:
                await consume_task
            for task in pending:
                task.cancel()
        except asyncio.CancelledError:
            session.cancel()
            consume_task.cancel()
            raise
        finally:
            if not cancel_task.done():
                cancel_task.cancel()

        if cancellation.is_set():
            return TurnResult.cancelled(request)
        if final is None:
            return TurnResult.failed(request, error="coding session produced no assistant message")
        if final.stop_reason in {"error", "aborted"}:
            return TurnResult.failed(
                request,
                error=final.error_message or f"assistant stopped with {final.stop_reason}",
                output=final.text,
            )
        return TurnResult.succeeded(
            request,
            output=final.text,
            metadata={"model": final.model, "stop_reason": final.stop_reason},
        )


__all__ = ["CodingSessionTurnRunner", "SessionResolver"]
