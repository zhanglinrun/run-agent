from __future__ import annotations

import pytest

from agents.providers.base import ModelResponse
from agents.runtime import AgentCore
from agents.runtime.contracts import ToolCall, ToolResult
from agents.runtime.ui import _EncodingSafeWriter


class _StrictGbkStream:
    encoding = "gbk"

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        text.encode(self.encoding, errors="strict")
        self.text += text
        return len(text)

    def flush(self) -> None:
        return None


def test_terminal_writer_replaces_characters_unsupported_by_gbk() -> None:
    stream = _StrictGbkStream()
    writer = _EncodingSafeWriter(stream)

    written = writer.write("\U0001f4c1 list_files: project")

    assert written == len(stream.text)
    assert "list_files" in stream.text
    assert "project" in stream.text
    assert "\U0001f4c1" not in stream.text


@pytest.mark.asyncio
async def test_openai_parallel_tool_calls_execute_once_each() -> None:
    class Provider:
        def __init__(self) -> None:
            self.responses = [
                ModelResponse(tool_calls=(
                    ToolCall("call-a", "read_file", {"file_path": "a.txt"}),
                    ToolCall("call-b", "read_file", {"file_path": "b.txt"}),
                )),
                ModelResponse(text="done", stop_reason="stop"),
            ]

        async def complete(self, request):
            return self.responses.pop(0)

    class Executor:
        def __init__(self) -> None:
            self.calls: list[ToolCall] = []

        async def execute(self, call: ToolCall) -> ToolResult:
            self.calls.append(call)
            return ToolResult(call.id, call.name, call.input["file_path"], True)

    executor = Executor()
    result = await AgentCore(provider=Provider(), tool_executor=executor, max_turns=2).run("read both")

    assert result.text == "done"
    assert [call.input["file_path"] for call in executor.calls] == ["a.txt", "b.txt"]
