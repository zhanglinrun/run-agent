from pathlib import Path
from types import SimpleNamespace
from typing import cast

from run_agent_coding.commands import CommandResult, SlashCommand, create_default_command_registry
from run_agent_coding.events import AgentSettledEvent, QueueUpdateEvent, SessionAgentEndEvent
from run_agent_coding.prompt_templates import PromptTemplate
from run_agent_coding.session import CodingSession
from run_agent_coding.skills import Skill
from run_agent_coding.tui.adapter import TuiEventAdapter
from run_agent_coding.tui.autocomplete import get_completions
from run_agent_coding.tui.state import TuiState
from run_agent_core.events import (
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from run_agent_core.messages import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from run_agent_core.provider_events import TextDeltaEvent, ThinkingDeltaEvent
from run_agent_core.tools import AgentToolResult


def test_streamed_cjk_is_replaced_by_canonical_blocks_with_stable_ids() -> None:
    state = TuiState()
    adapter = TuiEventAdapter(state)
    adapter.apply(AgentStartEvent())
    prompt = UserMessage(content="分析项目")
    adapter.apply(MessageStartEvent(message=prompt))
    adapter.apply(MessageEndEvent(message=prompt))
    partial = AssistantMessage()
    adapter.apply(MessageStartEvent(message=partial))
    for delta in ("中文", "不会截断\n"):
        adapter.apply(
            MessageUpdateEvent(
                message=partial,
                assistant_message_event=TextDeltaEvent(
                    content_index=1, delta=delta, partial=partial
                ),
            )
        )
    provisional_id = state.items[-1].id
    final = AssistantMessage(
        content=[
            ThinkingContent(thinking="先检查结构"),
            TextContent(text="中文不会截断\n完整表格与结尾"),
            ToolCall(id="read-1", name="read", arguments={"path": "README.md"}),
        ]
    )
    adapter.apply(MessageEndEvent(message=final))
    assert [item.role for item in state.items] == ["user", "thinking", "assistant", "tool"]
    assert state.items[2].text == final.text
    assert state.items[2].id == provisional_id
    assert not state.items[2].pending
    adapter.apply(SessionAgentEndEvent())
    assert state.running  # No success indicator before durable commit.
    adapter.apply(
        AgentSettledEvent(
            run_id="r",
            session_id="s",
            branch_id="b",
            status="succeeded",
            head_id=None,
            watermark=1,
        )
    )
    assert not state.running
    assert state.activity == "Ready"


def test_parallel_tool_updates_results_and_restored_projection_match() -> None:
    state = TuiState()
    adapter = TuiEventAdapter(state)
    calls = AssistantMessage(
        content=[
            ToolCall(id="a", name="read", arguments={"path": "a.py"}),
            ToolCall(id="b", name="bash", arguments={"command": "pytest"}),
        ]
    )
    adapter.apply(MessageEndEvent(message=calls))
    for call in calls.tool_calls:
        adapter.apply(
            ToolExecutionStartEvent(tool_call_id=call.id, tool_name=call.name, args=call.arguments)
        )
    adapter.apply(
        ToolExecutionUpdateEvent(
            tool_call_id="b",
            tool_name="bash",
            partial_result=AgentToolResult(content=[TextContent(text="running")]),
        )
    )
    results = [
        ToolResultMessage(
            tool_call_id="b", tool_name="bash", content="failed\n" * 150, is_error=True
        ),
        ToolResultMessage(tool_call_id="a", tool_name="read", content="print('中文')"),
    ]
    for result in results:
        adapter.apply(
            ToolExecutionEndEvent(
                tool_call_id=result.tool_call_id,
                tool_name=result.tool_name,
                result=AgentToolResult(content=result.content),
                is_error=result.is_error,
            )
        )
        adapter.apply(MessageEndEvent(message=result))
    assert len(state.items) == 2
    assert state.items[1].is_error and not state.items[1].pending
    assert state.items[1].result_text == "failed\n" * 150
    restored = TuiState(running=True, queued_steering=("old",), output_tokens=100)
    restored.load_messages([calls, *results])
    assert [(i.role, i.arguments, i.result_text, i.is_error) for i in restored.items] == [
        (i.role, i.arguments, i.result_text, i.is_error) for i in state.items
    ]
    assert not restored.running and not restored.queued_steering and not restored.output_tokens


def test_cancel_preserves_streamed_tail_and_queue_state() -> None:
    state = TuiState()
    adapter = TuiEventAdapter(state)
    adapter.apply(AgentStartEvent())
    adapter.apply(QueueUpdateEvent(steering=("先修测试",), follow_up=("然后总结",)))
    partial = AssistantMessage()
    adapter.apply(MessageStartEvent(message=partial))
    adapter.apply(
        MessageUpdateEvent(
            message=partial,
            assistant_message_event=ThinkingDeltaEvent(
                content_index=0, delta="检查中…", partial=partial
            ),
        )
    )
    adapter.apply(
        MessageUpdateEvent(
            message=partial,
            assistant_message_event=TextDeltaEvent(
                content_index=1, delta="未完成的中文回答", partial=partial
            ),
        )
    )
    adapter.apply(MessageEndEvent(message=AssistantMessage(stop_reason="aborted")))
    assert [i.text for i in state.items] == ["检查中…", "未完成的中文回答", "Cancelled"]
    assert not any(i.pending for i in state.items)
    assert state.queued_steering == ("先修测试",)
    assert state.queued_follow_up == ("然后总结",)


def test_dynamic_completion_includes_extensions_aliases_skills_and_templates() -> None:
    registry = create_default_command_registry()
    session = cast(
        CodingSession,
        SimpleNamespace(
            command_registry=registry,
            skills=[Skill("review", Path("SKILL.md"), "review", "Inspect code")],
            prompt_templates=[
                PromptTemplate("summarize", Path("summary.md"), "summarize", "Summary")
            ],
        ),
    )
    assert get_completions(session, "/approve") == []
    registry.register(
        SlashCommand(
            "approve",
            "Approve operation",
            "/approve",
            lambda _: CommandResult(handled=True),
            aliases=("allow",),
        )
    )
    assert get_completions(session, "/allow")[0].description == "Approve operation"
    assert get_completions(session, "/skill:r")[0].text == "/skill:review"
    assert get_completions(session, "/sum")[0].text == "/summarize"
    assert get_completions(session, "/clear")[0].description.startswith("Clear the visible")
    for prompt in (
        "ordinary prompt",
        "/model luna",
        "/model\t",
        "/skill:review\nargs",
        "//literal",
    ):
        assert get_completions(session, prompt) == []
