"""Exercise extension policy through the production agent loop."""

from collections.abc import Mapping

from pi_event_helpers import assistant_done
from run_agent_ai import FakeProvider
from run_agent_coding.extensions import ExtensionRuntime
from run_agent_core import AgentMessage, AgentTool, AssistantMessage, ToolCall, ToolResultMessage
from run_agent_core.loop import run_agent_loop
from run_agent_core.types import JSONValue


async def execute_extension_tool(
    runtime: ExtensionRuntime,
    tool: AgentTool,
    call_id: str,
    arguments: Mapping[str, JSONValue],
) -> ToolResultMessage:
    call = ToolCall(id=call_id, name=tool.name, arguments=dict(arguments))
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content=[call]), "toolUse")],
            [assistant_done(AssistantMessage(content="done"))],
        ]
    )
    messages: list[AgentMessage] = []
    async for _event in run_agent_loop(
        provider=provider,
        model="fake",
        system="test",
        messages=messages,
        tools=runtime.compose_tools([tool]),
        before_tool_call=runtime.before_tool_call,
        after_tool_call=runtime.after_tool_call,
        max_turns=2,
    ):
        pass
    return next(message for message in messages if isinstance(message, ToolResultMessage))
