"""Committed adoption of a staged extension runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from run_agent_coding.extensions.api import SessionLifecycleReason
from run_agent_coding.extensions.runtime import ExtensionRuntime, RuntimeCloseResult


@dataclass(frozen=True, slots=True)
class AdoptionCleanup:
    """Observable cleanup result after the successor has already committed."""

    cancelled: bool
    notice: str | None


async def finish_committed_adoption(
    runtime: ExtensionRuntime,
    reason: SessionLifecycleReason,
) -> AdoptionCleanup:
    """Notify and retire an outgoing runtime after successor publication.

    The caller must invoke this only after the successor's host publication has
    committed. Cancellation is contained until cleanup reaches a terminal state;
    a committed adoption is never reported as rolled back.
    """
    runtime.begin_retiring()
    await runtime.emit_session_shutdown(reason)
    runtime.clear_ui_status()
    task = asyncio.create_task(
        runtime.aclose(),
        name="run-agent-committed-extension-runtime-close",
    )
    cancelled = await _wait_through_cancellation(task)
    notice: str | None
    try:
        result = task.result()
    except BaseException as exc:
        notice = f"Previous extension cleanup failed: {type(exc).__name__}: {exc}"
    else:
        notice = _cleanup_notice(result)
    return AdoptionCleanup(cancelled=cancelled, notice=notice)


async def _wait_through_cancellation(task: asyncio.Task[RuntimeCloseResult]) -> bool:
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
        except BaseException:
            pass
        return cancelled


def _cleanup_notice(result: RuntimeCloseResult) -> str | None:
    if result.drained:
        return None
    details = "; ".join(result.cleanup_errors)
    return (
        f"Previous extension cleanup is still pending: {result.contained_managed_tasks} "
        f"managed tasks, {result.contained_discovery_tasks} provider callbacks, "
        f"{result.contained_disposers} disposers" + (f"; {details}" if details else "")
    )


__all__ = ["AdoptionCleanup", "finish_committed_adoption"]
