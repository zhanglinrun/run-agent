"""Capture tool definitions and Python entry-point implementation identities."""

from __future__ import annotations

import hashlib
import marshal
from collections.abc import Sequence
from types import CodeType

from run_agent_core.tools import AgentTool
from run_agent_core.types import JSONValue


def _normalize(code: CodeType) -> CodeType:
    return code.replace(
        co_filename="<tool>",
        co_consts=tuple(_normalize(value) if isinstance(value, CodeType) else value
                        for value in code.co_consts),
    )


def _implementation(value: object) -> str | None:
    if value is None:
        return None
    code = getattr(value, "__code__", None)
    if code is None and callable(value):
        code = getattr(value.__call__, "__code__", None)
    if not isinstance(code, CodeType):
        raise ValueError("Tool entry points must expose a Python implementation identity")
    return hashlib.sha256(marshal.dumps(_normalize(code))).hexdigest()


def tool_manifest(tools: Sequence[AgentTool]) -> list[JSONValue]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.parameters),
            "execution_mode": tool.execution_mode,
            "prompt_snippet": tool.prompt_snippet,
            "prompt_guidelines": list(tool.prompt_guidelines),
            "execute": _implementation(tool.execute_fn),
            "prepare": _implementation(tool.prepare_arguments),
        }
        for tool in tools
    ]
