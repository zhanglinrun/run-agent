"""The portable agent core, checked against the Pi behaviours it ports.

These are the seams the coding layer relies on without owning: how a transcript
is projected for the provider, how tool arguments are checked, what ``continue``
means, and how a run signals that it is idle.
"""

import asyncio

import pytest

from run_agent_ai.fake import FakeProvider
from run_agent_core.events import AgentEndEvent, MessageEndEvent, ToolExecutionEndEvent
from run_agent_core.harness import AgentHarness, AgentHarnessConfig
from run_agent_core.messages import (
    BRANCH_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_PREFIX,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
    convert_to_llm,
)
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.tools import AgentTool, AgentToolResult, validate_tool_arguments


def reply(text, *tool_calls):
    return [
        AssistantDoneEvent(
            reason="toolUse" if tool_calls else "stop",
            message=AssistantMessage(
                content=[TextContent(text=text), *tool_calls],
                model="test",
                provider="test",
                stop_reason="toolUse" if tool_calls else "stop",
            ),
        )
    ]


def echo_tool(**overrides):
    async def execute(tool_call_id, arguments, signal=None, on_update=None):
        return AgentToolResult(
            content=[TextContent(text=f"count={arguments.get('count')!r}")],
            usage=Usage(input=3, output=4, total_tokens=7),
        )

    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}, "label": {"type": "string"}},
        "required": ["count"],
    }
    return AgentTool(
        name="echo", label="Echo", description="", parameters=schema, execute_fn=execute
    )


def harness(provider, *, tools=(), **config):
    return AgentHarness(
        AgentHarnessConfig(
            provider=provider, model="test", system="sys", tools=list(tools), **config
        )
    )


async def drain(events):
    return [event async for event in events]


def test_convert_to_llm_projects_session_only_roles_the_way_pi_does():
    messages = [
        UserMessage(content="hi"),
        CompactionSummaryMessage(summary="earlier", tokens_before=10),
        BranchSummaryMessage(summary="elsewhere", from_id="x"),
        BashExecutionMessage(command="ls", output="a\nb", exit_code=1),
        BashExecutionMessage(command="pwd", output="/", exclude_from_context=True),
        CustomMessage(custom_type="note", content=[TextContent(text="kept blocks")]),
        AssistantMessage(content=[TextContent(text="ok")], model="m", stop_reason="stop"),
    ]
    converted = convert_to_llm(messages)
    assert [message.role for message in converted] == ["user"] * 5 + ["assistant"]
    assert converted[1].text.startswith(COMPACTION_SUMMARY_PREFIX)
    assert converted[1].text.endswith("</summary>")
    assert converted[2].text.startswith(BRANCH_SUMMARY_PREFIX)
    assert converted[3].text == "Ran `ls`\n```\na\nb\n```\n\nCommand exited with code 1"
    assert converted[4].content == [TextContent(text="kept blocks")]


def test_argument_validation_requires_keys_and_coerces_quoted_scalars():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "on": {"type": "boolean"},
            "name": {"type": ["string", "null"]},
        },
        "required": ["count"],
    }
    checked = validate_tool_arguments(
        schema, {"count": "3", "ratio": "0.5", "on": "true", "name": None, "extra": 1}
    )
    assert checked == {"count": 3, "ratio": 0.5, "on": True, "name": None, "extra": 1}
    with pytest.raises(ValueError, match="Missing required argument"):
        validate_tool_arguments(schema, {})
    with pytest.raises(ValueError, match="must be of type integer"):
        validate_tool_arguments(schema, {"count": "three"})


async def test_the_loop_rejects_invalid_arguments_and_forwards_tool_usage():
    provider = FakeProvider(
        [
            reply(
                "calling",
                ToolCall(id="bad", name="echo", arguments={}),
                ToolCall(id="good", name="echo", arguments={"count": "2"}),
            ),
            reply("done"),
        ]
    )
    agent = harness(provider, tools=(echo_tool(),))
    events = await drain(agent.prompt("go"))
    ends = {
        event.tool_call_id: event for event in events if isinstance(event, ToolExecutionEndEvent)
    }
    assert ends["bad"].is_error and "Missing required argument" in ends["bad"].result.text
    assert not ends["good"].is_error and ends["good"].result.text == "count=2"
    results = [message for message in agent.messages if isinstance(message, ToolResultMessage)]
    assert results[1].usage == Usage(input=3, output=4, total_tokens=7)
    # The provider saw only the three LLM roles, in order.
    assert [message.role for message in provider.calls[-1][2]] == [
        "user",
        "assistant",
        "toolResult",
        "toolResult",
    ]


async def test_continue_from_an_assistant_reply_drains_the_queues_or_refuses():
    provider = FakeProvider([reply("first"), reply("second")])
    agent = harness(provider)
    await drain(agent.prompt("start"))
    with pytest.raises(ValueError, match="Cannot continue from message role: assistant"):
        agent.continue_()
    agent.follow_up("later")
    events = await drain(agent.continue_())
    assert isinstance(events[-1], AgentEndEvent)
    assert [message.text for message in agent.messages if isinstance(message, UserMessage)] == [
        "start",
        "later",
    ]


async def test_wait_for_idle_reset_and_run_failure_as_message():
    class Exploding:
        def stream_response(self, **kwargs):
            raise RuntimeError("boom")

    agent = harness(Exploding(), run_failure="message")
    await agent.wait_for_idle()
    events = await drain(agent.prompt("hi"))
    final = [event.message for event in events if isinstance(event, MessageEndEvent)][-1]
    assert isinstance(final, AssistantMessage)
    assert final.stop_reason == "error" and final.error_message == "boom"
    assert isinstance(events[-1], AgentEndEvent)
    await asyncio.wait_for(agent.wait_for_idle(), 1)
    agent.steer("x")
    agent.reset()
    assert agent.messages == () and not agent.has_queued_messages()

    strict = harness(Exploding())
    with pytest.raises(RuntimeError, match="boom"):
        await drain(strict.prompt("hi"))


async def test_independent_queue_modes():
    provider = FakeProvider([reply("a"), reply("b"), reply("c")])
    agent = harness(provider, steering_mode="all", follow_up_mode="one_at_a_time")
    agent.steer("s1")
    agent.steer("s2")
    agent.follow_up("f1")
    agent.follow_up("f2")
    await drain(agent.prompt("p"))
    # Steering in "all" mode lands in one turn; follow-ups arrive one per continuation.
    first_turn = [m.text for m in provider.calls[0][2] if isinstance(m, UserMessage)]
    assert first_turn == ["p", "s1", "s2"]
    assert len(provider.calls) == 3
    assert [m.text for m in agent.messages if isinstance(m, UserMessage)] == [
        "p",
        "s1",
        "s2",
        "f1",
        "f2",
    ]
    assert not agent.has_queued_messages()
