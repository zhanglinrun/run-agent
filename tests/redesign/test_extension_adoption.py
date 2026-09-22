from __future__ import annotations

import asyncio

import pytest

from run_agent_coding.extensions.adoption import finish_committed_adoption
from run_agent_coding.extensions.api import ExtensionError, ExtensionGeneration
from run_agent_coding.extensions.runtime import RuntimeCloseResult


def test_generation_retiring_is_read_only_then_stale() -> None:
    parent = ExtensionGeneration()
    child = ExtensionGeneration(parent=parent)

    parent.begin_retiring()
    assert parent.state == "retiring"
    assert child.state == "retiring"
    child.assert_readable()
    with pytest.raises(ExtensionError):
        child.assert_active()

    parent.invalidate()
    assert child.state == "retired"
    with pytest.raises(ExtensionError):
        child.assert_readable()


@pytest.mark.anyio
async def test_committed_adoption_orders_notification_before_cleanup() -> None:
    events: list[str] = []

    class Runtime:
        def begin_retiring(self) -> None:
            events.append("retiring")

        async def emit_session_shutdown(self, reason: str) -> None:
            events.append(f"shutdown:{reason}")

        def clear_ui_status(self) -> None:
            events.append("clear-ui")

        async def aclose(self) -> RuntimeCloseResult:
            events.append("close")
            return RuntimeCloseResult(drained=True)

    result = await finish_committed_adoption(Runtime(), "reload")  # type: ignore[arg-type]
    assert result.notice is None
    assert result.cancelled is False
    assert events == ["retiring", "shutdown:reload", "clear-ui", "close"]


@pytest.mark.anyio
async def test_committed_cleanup_contains_caller_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Runtime:
        def begin_retiring(self) -> None:
            pass

        async def emit_session_shutdown(self, reason: str) -> None:
            del reason

        def clear_ui_status(self) -> None:
            pass

        async def aclose(self) -> RuntimeCloseResult:
            entered.set()
            await release.wait()
            return RuntimeCloseResult(drained=True)

    adoption = asyncio.create_task(
        finish_committed_adoption(Runtime(), "reload")  # type: ignore[arg-type]
    )
    await entered.wait()
    adoption.cancel()
    await asyncio.sleep(0)
    assert not adoption.done()
    release.set()
    result = await adoption
    assert result.cancelled is True
