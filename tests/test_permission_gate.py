"""PermissionGate tests ported from my-pi-agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

from run_agent_coding.extensions import ToolCallHookEvent
from run_agent_extensions.permission_policy.permissions import PermissionGate, PermissionRequest


def _hook(tool_name: str, arguments: dict, *, tool_call_id: str = "c1") -> ToolCallHookEvent:
    return ToolCallHookEvent(tool_name=tool_name, arguments=arguments, tool_call_id=tool_call_id)


async def test_permission_gate_readonly_tools_always_allowed():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="review", confirm_callback=mock_cb)
    for tool_name in ("read", "grep", "find"):
        result = await gate(_hook(tool_name, {"path": "a.txt"}))
        assert result.block is False
    mock_cb.assert_not_called()


async def test_permission_gate_yolo_mode_allows_all():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="yolo", confirm_callback=mock_cb)
    result = await gate(_hook("write", {"path": "a.txt", "content": "123"}, tool_call_id="c2"))
    assert result.block is False
    mock_cb.assert_not_called()


async def test_permission_gate_autonomous_mode_allows_all():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="autonomous", confirm_callback=mock_cb)
    result = await gate(_hook("bash", {"command": "rm -rf /"}, tool_call_id="c2_auto"))
    assert result.block is False
    mock_cb.assert_not_called()


async def test_permission_gate_safe_bash_commands_allowed():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="review", confirm_callback=mock_cb)
    for cmd in (
        "git status",
        "git diff main",
        "git log -n 5",
        "pytest -q",
        "python -m pytest tests/",
        "uv run pytest",
    ):
        result = await gate(_hook("bash", {"command": cmd}, tool_call_id="c_safe"))
        assert result.block is False
    mock_cb.assert_not_called()


async def test_permission_gate_review_mode_prompts_write():
    captured_req: PermissionRequest | None = None

    async def cb(req: PermissionRequest) -> bool:
        nonlocal captured_req
        captured_req = req
        return True

    gate = PermissionGate(mode="review", confirm_callback=cb)
    result = await gate(
        _hook("write", {"path": "a.txt", "content": "hello world"}, tool_call_id="c3")
    )
    assert result.block is False
    assert captured_req is not None
    assert captured_req.action == "write"
    assert captured_req.target == "a.txt"
    assert captured_req.preview == "hello world"
    assert captured_req.details == {"path": "a.txt", "content": "hello world"}


async def test_permission_gate_review_mode_prompts_edit():
    captured_req: PermissionRequest | None = None

    async def cb(req: PermissionRequest) -> bool:
        nonlocal captured_req
        captured_req = req
        return True

    gate = PermissionGate(mode="review", confirm_callback=cb)
    result = await gate(
        _hook(
            "edit",
            {"path": "src/main.py", "edits": [{"oldText": "a", "newText": "b"}]},
            tool_call_id="c3_edit",
        )
    )
    assert result.block is False
    assert captured_req is not None
    assert captured_req.action == "edit"
    assert captured_req.target == "src/main.py"
    assert captured_req.preview == "[{'oldText': 'a', 'newText': 'b'}]"


async def test_permission_gate_review_mode_prompts_unsafe_bash():
    mock_cb = AsyncMock(return_value=True)
    gate = PermissionGate(mode="review", confirm_callback=mock_cb)
    result = await gate(_hook("bash", {"command": "rm -rf /tmp/test"}, tool_call_id="c_unsafe"))
    assert result.block is False
    mock_cb.assert_called_once()
    req = mock_cb.call_args[0][0]
    assert req.action == "bash"
    assert req.target == "rm -rf /tmp/test"


async def test_permission_gate_denial_blocks_tool():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="review", confirm_callback=mock_cb)
    result = await gate(_hook("write", {"path": "a.txt", "content": "123"}, tool_call_id="c4"))
    assert result.block is True
    assert "用户拒绝了工具 [write] 的执行请求。" in (result.reason or "")


async def test_permission_gate_no_callback_allows_by_default():
    gate = PermissionGate(mode="review", confirm_callback=None)
    result = await gate(_hook("write", {"path": "a.txt", "content": "123"}, tool_call_id="c5"))
    assert result.block is False


async def test_permission_gate_strict_mode_blocks_readonly_without_approval():
    mock_cb = AsyncMock(return_value=False)
    gate = PermissionGate(mode="strict", confirm_callback=mock_cb)
    result = await gate(_hook("read", {"path": "secret.txt"}, tool_call_id="c6"))
    assert result.block is True
    assert "用户拒绝了工具 [read] 的执行请求。" in (result.reason or "")
    mock_cb.assert_called_once()


async def test_permission_gate_sync_callback_supported():
    def sync_cb(req: PermissionRequest) -> bool:
        return False

    gate = PermissionGate(mode="review", confirm_callback=sync_cb)
    result = await gate(_hook("write", {"path": "a.txt", "content": "123"}, tool_call_id="c7"))
    assert result.block is True
    assert "用户拒绝了工具 [write] 的执行请求。" in (result.reason or "")
