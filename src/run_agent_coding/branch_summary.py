"""Model-assisted summaries for abandoned session-tree branches."""

from __future__ import annotations

from collections.abc import Sequence

from run_agent_coding.branch_summary_format import (
    add_branch_summary_context,
    branch_summary_prompt,
)
from run_agent_core.messages import (
    AgentMessage,
    AssistantMessage,
    UserMessage,
)
from run_agent_core.provider import BeforeModelRequest, ModelProvider, ModelRequest
from run_agent_core.provider_events import AssistantDoneEvent, AssistantErrorEvent

BRANCH_SUMMARY_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a structured summary "
    "following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the structured summary."
)


async def summarize_branch_messages_with_model(
    *,
    provider: ModelProvider,
    model: str,
    messages: Sequence[AgentMessage],
    custom_instructions: str | None = None,
    replace_instructions: bool = False,
    before_model_request: BeforeModelRequest | None = None,
    session_id: str | None = None,
) -> str | None:
    """Return a model-generated branch summary, or None when generation fails."""
    if not messages:
        return None

    response: AssistantMessage | None = None
    request_messages: list[AgentMessage] = [
        UserMessage(
            content=branch_summary_prompt(
                messages,
                custom_instructions=custom_instructions,
                replace_instructions=replace_instructions,
            )
        )
    ]
    system_prompt = BRANCH_SUMMARY_SYSTEM_PROMPT
    if before_model_request is not None:
        replaced = await before_model_request(
            ModelRequest(
                model,
                system_prompt,
                request_messages,
                (),
                session_id,
            )
        )
        if isinstance(replaced, ModelRequest):
            model = replaced.model
            system_prompt = replaced.system
            request_messages = list(replaced.messages)
            if replaced.session_id is not None:
                session_id = replaced.session_id
    async for event in provider.stream_response(
        model=model,
        system=system_prompt,
        messages=request_messages,
        tools=[],
        session_id=session_id,
    ):
        if isinstance(event, AssistantErrorEvent):
            return None
        if isinstance(event, AssistantDoneEvent):
            response = event.message

    if response is None:
        return None
    summary = response.text.strip()
    if not summary:
        return None
    return add_branch_summary_context(summary, messages)
