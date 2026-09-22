"""Committed adoption and branch reuse of a staged extension runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from run_agent_coding.extensions.api import SessionLifecycleReason
from run_agent_coding.extensions.runtime import ExtensionRuntime, RuntimeCloseResult
from run_agent_coding.host.context_resources import ExtensionResourceSnapshot
from run_agent_coding.host.contracts import SessionActivation
from run_agent_coding.storage.settle import settle
from run_agent_core.session import BranchSummaryEntry, LeafEntry
from run_agent_core.session.entries import SessionEntry

if TYPE_CHECKING:
    from run_agent_coding.jsonl_storage import SessionWriter
    from run_agent_coding.session import SessionResources


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


@dataclass(frozen=True, slots=True)
class BranchReuseStaging:
    """Staged branch activation for a runtime that stays live until publication."""

    activation: SessionActivation
    resources: SessionResources
    extension_resources: ExtensionResourceSnapshot | None


@dataclass(frozen=True, slots=True)
class BranchReuseCommit:
    """Committed branch reuse: the marker and leaf now on the active path."""

    marker_id: str
    leaf_id: str


class BranchReuseSession(Protocol):
    """The coding-session surface branch reuse needs, and nothing else."""

    @property
    def _extension_runtime(self) -> ExtensionRuntime: ...

    @property
    def storage(self) -> SessionWriter: ...

    _last_parent_id: str | None
    _resource_snapshot_id: str | None

    async def _prepare_resource_activation(
        self,
        reason: str,
        *,
        resources: SessionResources | None = None,
        extension_resources: ExtensionResourceSnapshot | None = None,
    ) -> SessionActivation: ...

    async def _reload_entry_cache(self) -> None: ...

    def _use_resources(self, resources: SessionResources) -> None: ...


async def stage_branch_reuse(
    session: BranchReuseSession,
    *,
    resources: SessionResources,
    extension_resources: ExtensionResourceSnapshot | None,
) -> BranchReuseStaging:
    """Stage a branch that reuses the active runtime; never retires it.

    Contract, shared with the staged-then-committed semantics of
    `finish_committed_adoption`:

    - A branch is not a generation change. The active runtime is reused, so the
      outgoing side receives no `begin_retiring`, no `session_shutdown`, no UI
      clear, and no disposer run.
    - Staging only re-selects and verifies resources against the live runtime.
      Any failure here leaves the old tools, MCP connections, UI state and the
      active generation exactly as they were.
    - The publication point is the resource re-selection succeeding plus the
      accepted `storage.fork` in `publish_branch_reuse`; callers must not
      report the branch as failed once that returned.
    """
    activation = await session._prepare_resource_activation(
        "branch",
        resources=resources,
        extension_resources=extension_resources,
    )
    return BranchReuseStaging(
        activation=activation,
        resources=resources,
        extension_resources=extension_resources,
    )


async def publish_branch_reuse(
    session: BranchReuseSession,
    staging: BranchReuseStaging,
    *,
    target_id: str | None,
    branch_point: str | None,
    summary_entry: BranchSummaryEntry | None = None,
) -> BranchReuseCommit:
    """Commit a staged branch onto the active path of the reused runtime.

    Publication is the accepted `storage.fork`: the branch marker, leaf and the
    staged resources are applied only after it succeeded. A failure before that
    point leaves the reused runtime's tools, MCP connections, UI state and
    generation fully intact; the caller must not have retired the runtime.
    """
    marker = staging.activation.entry.model_copy(update={"parent_id": target_id})
    leaf = LeafEntry(parent_id=marker.id, entry_id=marker.id)
    entries: Sequence[SessionEntry] = (
        (summary_entry, marker, leaf) if summary_entry is not None else (marker, leaf)
    )
    await session.storage.fork(
        branch_point,
        token=session.storage.token,
        entries=entries,
    )
    await settle(session._reload_entry_cache())
    session._last_parent_id = marker.id
    session._resource_snapshot_id = marker.id
    session._extension_runtime.context_resources.snapshot = staging.extension_resources
    session._use_resources(staging.resources)
    return BranchReuseCommit(marker_id=marker.id, leaf_id=marker.id)


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


__all__ = [
    "AdoptionCleanup",
    "BranchReuseCommit",
    "BranchReuseSession",
    "BranchReuseStaging",
    "finish_committed_adoption",
    "publish_branch_reuse",
    "stage_branch_reuse",
]
