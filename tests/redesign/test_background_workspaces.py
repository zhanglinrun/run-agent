import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from tests.redesign.git_helpers import create_repository, git
from tests.redesign.test_coding_application import ReplyProvider
from tests.redesign.test_gateway_runtime import eventually, released, runtime, submit

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import ArtifactRef
from run_agent_coding.storage.artifacts import ArtifactStore
from run_agent_coding.storage.backup import create_backup, verify_backup
from run_agent_coding.storage.handle import SqliteSessionHandle
from run_agent_core.messages import AssistantMessage, ToolCall, UserMessage
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_core.session.entries import MessageEntry
from run_agent_core.session.memory import SessionState
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.controller import SessionController
from run_agent_gateway.gateway import AgentGateway, InboundMessage, QueueGatewayAdapter
from run_agent_gateway.identity import IdentityPolicy, IdentityRule
from run_agent_gateway.scheduler import GatewayScheduler
from run_agent_gateway.workspaces import BackgroundWorkspaces, WorkspaceError

__all__ = ["runtime"]


@pytest.fixture
def workspace(tmp_path):
    return create_repository(tmp_path / "project")


async def test_worktree_is_pinned_and_result_includes_patch_and_new_files(tmp_path, workspace):
    service = BackgroundWorkspaces(tmp_path / "worktrees", ArtifactStore(tmp_path / "artifacts"))
    revision = await service.capture(workspace)
    task_id = uuid4().hex
    (workspace / "tracked.txt").write_text("new main commit\n", encoding="utf-8")
    git(workspace, "commit", "-am", "new main commit")
    isolated = await service.materialize(task_id, revision)
    assert isolated != workspace and (isolated / "tracked.txt").read_text() == "original\n"
    assert await service.materialize(task_id, revision) == isolated
    (isolated / "tracked.txt").write_text("background edit\n", encoding="utf-8")
    (isolated / "new.bin").write_bytes(b"\x00background\xff")
    report = await service.collect(task_id, revision)
    patch = await service.artifacts.read(ArtifactRef(**report["patch"]))
    assert b"background edit" in patch and b"original" in patch
    assert report["untracked"][0]["path"] == "new.bin"
    assert (
        await service.artifacts.read(ArtifactRef(**report["untracked"][0]["artifact"]))
        == b"\x00background\xff"
    )
    assert json.loads(await service.artifacts.read(ArtifactRef(**report["manifest"]))) == {
        key: value for key, value in report.items() if key != "manifest"
    }
    assert (workspace / "tracked.txt").read_text() == "new main commit\n"
    with pytest.raises(WorkspaceError, match="unreviewed"):
        await service.materialize(task_id, revision)
    with pytest.raises(WorkspaceError, match="still contains"):
        await service.discard_clean(task_id, revision)


async def test_dirty_non_git_and_path_escape_are_explicit(tmp_path, workspace):
    service = BackgroundWorkspaces(tmp_path / "worktrees", ArtifactStore(tmp_path / "artifacts"))
    with pytest.raises(WorkspaceError):
        await service.capture(tmp_path)
    (workspace / "untracked").write_text("not captured", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="clean"):
        await service.capture(workspace)
    (workspace / "untracked").unlink()
    revision = await service.capture(workspace)
    with pytest.raises(WorkspaceError, match="identity"):
        service.workspace("../outside", revision)
    with pytest.raises(WorkspaceError, match="working directory"):
        service.workspace(uuid4().hex, replace(revision, relative_cwd="../outside"))
    identity = uuid4().hex
    await service.materialize(identity, revision)
    await service.discard_clean(identity, revision)
    assert not service.directory(identity).exists()


async def test_background_uses_fixed_history_skill_project_and_original_destination(
    runtime, workspace
):
    repo, owner, host = runtime
    skill = host.options.paths.home / "skills" / "frozen" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Version one", encoding="utf-8")
    first = await repo.admit(owner, submit(workspace, text="source history"), model="test")
    first_run = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(first_run, asyncio.Event())
    await repo.release(owner, first_run)
    source = await repo.sessions.get_session(first.session_id)
    background = await repo.admit(
        owner,
        replace(submit(workspace, "background", "do isolated work"), lane="background"),
        model="test",
    )
    record = await repo.sessions.get_session(background.session_id)
    assert record.project_id == source.project_id and record.session_id != source.session_id
    assert record.metadata["source_resource_entry_id"]
    state = await repo.task(background.task_id, principal_id="alice")
    assert state["source_head_id"] == record.metadata["source_head_id"]
    assert state["resource_entry_id"] == record.metadata["source_resource_entry_id"]
    skill.write_text("Version two", encoding="utf-8")
    controller = SessionController(repo)
    await controller.handle(owner, submit(workspace, "new", "/new"), model="test")
    await controller.reconcile(owner)
    fresh = await repo.admit(owner, submit(workspace, "fresh", "new conversation"), model="test")
    assert fresh.session_id != first.session_id
    provider_inputs = []

    class EditingProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            provider_inputs.append(kwargs)
            path = Path(record.cwd)
            (path / "tracked.txt").write_text("changed by background\n", encoding="utf-8")
            (path / "report.txt").write_text("background report\n", encoding="utf-8")
            yield AssistantDoneEvent(
                reason="stop",
                message=AssistantMessage(
                    content="background done",
                    model="test",
                    provider="test",
                    stop_reason="stop",
                ),
            )

    host.provider_factory = lambda assignment: (
        EditingProvider() if assignment.lane == "background" else ReplyProvider()
    )
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    await scheduler.start()
    try:
        await eventually(lambda: released(repo, background.task_id))
        await eventually(lambda: released(repo, fresh.task_id))
        assert not scheduler.errors and scheduler.failure is None
        done = await repo.task(background.task_id, principal_id="alice")
        assert done["status"] == "succeeded", done
        assert "source history" in str(provider_inputs)
        assert "Version two" not in str(provider_inputs)
        background_entries = (await repo.sessions.read_entries(background.session_id)).entries
        user_text = [
            e.message.text
            for e in background_entries
            if isinstance(e, MessageEntry) and isinstance(e.message, UserMessage)
        ]
        assert user_text == ["source history", "do isolated work"]
        new_entries = (await repo.sessions.read_entries(fresh.session_id)).entries
        assert not any(
            isinstance(e, MessageEntry) and e.message.text == "background done" for e in new_entries
        )
        result = await repo.database.run(
            lambda c: c.execute(
                "SELECT * FROM gateway_outbox WHERE task_id=? AND kind='result'",
                (background.task_id,),
            ).fetchone()
        )
        body = json.loads(result["content_json"])
        assert body["origin_session_id"] == first.session_id and body["conversation_epoch"] == 1
        assert body["artifacts"]["manifest"] and body["artifacts"]["untracked"]
        assert (workspace / "tracked.txt").read_text() == "original\n"
        assert not (workspace / "report.txt").exists()
    finally:
        await scheduler.shutdown()


async def test_background_dirty_rejection_and_duplicate_after_source_changes(runtime, workspace):
    repo, owner, _ = runtime
    submission = replace(submit(workspace), lane="background")
    (workspace / "tracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="clean"):
        await repo.admit(owner, submission, model="test")
    assert (
        await repo.database.run(
            lambda c: c.execute("SELECT COUNT(*) FROM gateway_tasks").fetchone()[0]
        )
        == 0
    )
    (workspace / "tracked.txt").write_text("original\n", encoding="utf-8")
    receipt = await repo.admit(owner, submission, model="test")
    (workspace / "tracked.txt").write_text("later dirty", encoding="utf-8")
    duplicate = await repo.admit(owner, submission, model="test")
    assert duplicate.duplicate and duplicate.task_id == receipt.task_id


async def test_channel_background_control_runs_after_prepared_source(runtime, workspace):
    repo, owner, host = runtime
    first = await repo.admit(owner, submit(workspace), model="test")
    first_run = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(first_run, asyncio.Event())
    await repo.release(owner, first_run)
    # Avoid replaying the earlier receipt in the channel assertions.
    await repo.database.run(
        lambda c: c.execute("UPDATE gateway_outbox SET status='sent'"), write=True
    )
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    adapter = QueueGatewayAdapter("local")
    gateway = AgentGateway(
        scheduler,
        [adapter],
        IdentityPolicy((IdentityRule("local", "account", "alice", "alice", workspace),)),
        model="test",
        prepare_background=host.prepare_background,
    )
    await gateway.start()
    try:
        await adapter.receive_message(
            InboundMessage("bg", "account", "alice", "chat", "/background isolated")
        )
        accepted = await asyncio.wait_for(adapter.next_sent(), 10)
        result = await asyncio.wait_for(adapter.next_sent(), 10)
        assert accepted.content["lane"] == "background", accepted.content
        assert result.content["status"] == "succeeded", result.content
        assert result.content["origin_session_id"] == first.session_id
        assert result.content["session_id"] != first.session_id
        assert result.content["artifacts"]["manifest"]
    finally:
        await gateway.shutdown()


async def test_first_background_prepares_resources_without_model_call(
    runtime, workspace, monkeypatch
):
    repo, owner, host = runtime
    original = CodingApplication.open.__func__

    class NoCalls(ReplyProvider):
        async def stream_response(self, **kwargs):
            raise AssertionError("Resource preparation must not call a model")
            yield

    async def open_without_network(cls, options, **kwargs):
        return await original(cls, options, **{**kwargs, "provider": NoCalls()})

    monkeypatch.setattr(CodingApplication, "open", classmethod(open_without_network))
    value = replace(submit(workspace), lane="background")
    await host.prepare_background(value)
    receipt = await repo.admit(owner, value, model="test")
    state = await repo.task(receipt.task_id, principal_id="alice")
    assert state["resource_entry_id"] is not None
    assert (
        await repo.database.run(
            lambda c: c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        )
        == 0
    )
    monkeypatch.setattr(CodingApplication, "open", classmethod(original))
    assignment = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(assignment, asyncio.Event())
    await repo.release(owner, assignment)
    assert (await repo.task(receipt.task_id, principal_id="alice"))["status"] == "succeeded"
    await host.prepare_background(value)
    assert (await repo.admit(owner, value, model="test")).duplicate


async def test_background_cancellation_keeps_outputs_and_releases_after_cleanup(runtime, workspace):
    repo, owner, host = runtime
    first = await repo.admit(owner, submit(workspace), model="test")
    initial = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(initial, asyncio.Event())
    await repo.release(owner, initial)
    background = await repo.admit(
        owner, replace(submit(workspace, "bg"), lane="background"), model="test"
    )
    started = asyncio.Event()

    class WaitingProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            state = await repo.task(background.task_id, principal_id="alice")
            path = await repo.database.run(
                lambda c: c.execute(
                    "SELECT path FROM gateway_workspaces WHERE workspace_id=?",
                    (state["workspace_id"],),
                ).fetchone()[0]
            )
            (Path(path) / "partial.txt").write_text("partial output", encoding="utf-8")
            started.set()
            await asyncio.Event().wait()
            yield

    host.provider_factory = lambda _: WaitingProvider()
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    await scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), 10)
        await repo.cancel(owner, background.task_id, principal_id="alice")
        scheduler.signal_cancel(background.task_id)
        await eventually(lambda: released(repo, background.task_id))
        assert not scheduler.errors and scheduler.failure is None
        state = await repo.task(background.task_id, principal_id="alice")
        assert state["status"] == "cancelled"
        report = json.loads(state["artifacts_json"])
        assert report["untracked"][0]["path"] == "partial.txt"
        backup = await create_backup(
            repo.database, repo.workspaces.artifacts, workspace.parent / "backup"
        )
        manifest = await verify_backup(backup)
        assert report["manifest"] in manifest["artifacts"]
        assert (await repo.task(first.task_id, principal_id="alice"))["status"] == "succeeded"
    finally:
        await scheduler.shutdown()


async def test_result_includes_commits_made_inside_detached_worktree(tmp_path, workspace):
    service = BackgroundWorkspaces(tmp_path / "worktrees", ArtifactStore(tmp_path / "artifacts"))
    revision = await service.capture(workspace)
    identity = uuid4().hex
    isolated = await service.materialize(identity, revision)
    (isolated / "tracked.txt").write_text("committed background work\n", encoding="utf-8")
    git(isolated, "commit", "-am", "background change")
    report = await service.collect(identity, revision)
    assert report["result_commit"] != revision.commit
    assert b"committed background work" in await service.artifacts.read(
        ArtifactRef(**report["patch"])
    )
    assert git(workspace, "rev-parse", "HEAD").decode().strip() == revision.commit
    with pytest.raises(WorkspaceError, match="HEAD"):
        await service.discard_clean(identity, revision)


async def test_failed_background_admission_leaves_no_clone_or_worktree(runtime, workspace):
    repo, owner, _ = runtime

    def fail(point):
        if point == "gateway_task_inserted":
            raise RuntimeError("injected admission failure")

    repo.fault = fail
    with pytest.raises(RuntimeError, match="injected"):
        await repo.admit(owner, replace(submit(workspace), lane="background"), model="test")
    assert (
        await repo.database.run(lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        == 0
    )
    assert not repo.workspaces.root.exists()


@pytest.mark.parametrize("command", ["/stop", "/new"])
async def test_controls_remain_responsive_and_fence_pending_background_preparation(
    runtime, workspace, command
):
    repo, owner, host = runtime
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def prepare(submission):
        entered.set()
        await proceed.wait()

    adapter = QueueGatewayAdapter("local")
    gateway = AgentGateway(
        GatewayScheduler(repo, owner, CodingAssignmentRunner(host)),
        [adapter],
        IdentityPolicy((IdentityRule("local", "account", "alice", "alice", workspace),)),
        model="test",
        prepare_background=prepare,
    )
    await gateway.start()
    try:
        await adapter.receive_message(
            InboundMessage("bg", "account", "alice", "chat", "/background job")
        )
        await asyncio.wait_for(entered.wait(), 5)
        await adapter.receive_message(
            InboundMessage("status", "account", "alice", "chat", "/status")
        )
        status = await asyncio.wait_for(adapter.next_sent(), 5)
        assert status.content["status"] == "status"
        await adapter.receive_message(InboundMessage("stop", "account", "alice", "chat", command))
        stopped = await asyncio.wait_for(adapter.next_sent(), 5)
        assert stopped.content["status"] in {"stopped", "stopping"}
        proceed.set()
        deliveries = [
            await asyncio.wait_for(adapter.next_sent(), 10)
            for _ in range(2 if command == "/new" else 1)
        ]
        if command == "/new":
            assert any(d.content["status"] == "new_session" for d in deliveries)
        rejected = next(d for d in deliveries if d.content["status"] == "rejected")
        assert rejected.content["status"] == "rejected"
        assert (
            "changed or stopped" in rejected.content["error"]
            or "Previous session is stopping" in rejected.content["error"]
        )
        assert (
            await repo.database.run(
                lambda c: c.execute("SELECT COUNT(*) FROM gateway_tasks").fetchone()[0]
            )
            == 0
        )
    finally:
        proceed.set()
        await gateway.shutdown()


async def test_background_subdirectory_keeps_relative_working_directory(tmp_path, workspace):
    folder = workspace / "package"
    folder.mkdir()
    (folder / "module.py").write_text("value = 1\n", encoding="utf-8")
    git(workspace, "add", "package")
    git(workspace, "commit", "-m", "package")
    service = BackgroundWorkspaces(tmp_path / "worktrees", ArtifactStore(tmp_path / "artifacts"))
    revision = await service.capture(folder)
    assert revision.relative_cwd == "package"
    identity = uuid4().hex
    path = await service.materialize(identity, revision)
    assert path == service.directory(identity) / "package"
    assert (path / "module.py").read_text() == "value = 1\n"
    await service.discard_clean(identity, revision)


async def test_cloned_background_ancestry_excludes_off_branch_messages(runtime, workspace):
    repo, owner, _ = runtime
    first = await repo.admit(owner, submit(workspace), model="test")
    token = await repo.sessions.claim(first.session_id, owner_id=owner.owner_id, run_id="seed")
    storage = SqliteSessionHandle(repo.sessions, token, "main")
    try:
        a = MessageEntry(id="a", message=UserMessage(content="ancestor"))
        b = MessageEntry(id="b", parent_id="a", message=UserMessage(content="other branch"))
        await storage.append_entries((a, b), expected_head=None, token=token)
        await storage.fork("a", token=token)
        c = MessageEntry(id="c", parent_id="a", message=UserMessage(content="chosen branch"))
        await storage.append_entries((c,), expected_head="a", token=token)
        background = await repo.admit(
            owner, replace(submit(workspace, "background"), lane="background"), model="test"
        )
        entries = (await repo.sessions.read_entries(background.session_id)).entries
        assert [m.text for m in SessionState.from_entries(list(entries)).messages] == [
            "ancestor",
            "chosen branch",
        ]
        d = MessageEntry(id="d", parent_id="c", message=UserMessage(content="later source"))
        await storage.append_entries((d,), expected_head="c", token=token)
        assert {
            e.id for e in (await repo.sessions.read_entries(background.session_id)).entries
        }.isdisjoint({"b", "d"})
        assert (
            await repo.database.run(lambda c: c.execute("PRAGMA foreign_key_check").fetchall())
            == []
        )
    finally:
        await storage.aclose()


async def test_background_preparation_reserves_source_until_application_closes(runtime, workspace):
    repo, owner, _ = runtime
    value = replace(submit(workspace), lane="background")
    anchor = await repo.background_anchor(owner, value, model="test")
    ordinary = await repo.admit(owner, submit(workspace, "ordinary"), model="test")
    assert await repo.claim_next(owner) is None
    assert ordinary.session_id == anchor[0]
    await repo.release_background_anchor(owner, value.route)
    assert (await repo.claim_next(owner)).task_id == ordinary.task_id


async def test_background_real_write_tool_changes_only_worktree_and_is_archived(runtime, workspace):
    repo, owner, host = runtime
    await repo.admit(owner, submit(workspace), model="test")
    source_run = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(source_run, asyncio.Event())
    await repo.release(owner, source_run)
    receipt = await repo.admit(
        owner, replace(submit(workspace, "bg"), lane="background"), model="test"
    )

    class ToolProvider(ReplyProvider):
        calls = 0

        async def stream_response(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield AssistantDoneEvent(
                    reason="toolUse",
                    message=AssistantMessage(
                        model="test",
                        provider="test",
                        stop_reason="toolUse",
                        content=[
                            ToolCall(
                                id="write-worktree",
                                name="write",
                                arguments={"path": "tracked.txt", "content": "real write tool\n"},
                            )
                        ],
                    ),
                )
            else:
                yield AssistantDoneEvent(
                    reason="stop",
                    message=AssistantMessage(
                        model="test",
                        provider="test",
                        stop_reason="stop",
                        content="written",
                    ),
                )

    host.provider_factory = lambda _: ToolProvider()
    assignment = await repo.claim_next(owner)
    await CodingAssignmentRunner(host).run(assignment, asyncio.Event())
    await repo.release(owner, assignment)
    state = await repo.task(receipt.task_id, principal_id="alice")
    assert state["status"] == "succeeded"
    report = json.loads(state["artifacts_json"])
    assert b"real write tool" in await repo.workspaces.artifacts.read(
        ArtifactRef(**report["patch"])
    )
    assert (workspace / "tracked.txt").read_text() == "original\n"


async def test_preparation_cancelled_before_start_releases_its_reservation(runtime, workspace):
    repo, owner, host = runtime
    value = replace(submit(workspace), lane="background")
    await repo.background_anchor(owner, value, model="test")
    gateway = AgentGateway(
        GatewayScheduler(repo, owner, CodingAssignmentRunner(host)), [], IdentityPolicy(()),
        model="test",
    )

    async def unstarted():
        pytest.fail("Preparation was cancelled before it started")

    task = asyncio.create_task(unstarted())
    gateway._preparations.add(task)
    task.cancel()
    await gateway._finish_preparation(task, value.route)
    assert not gateway._preparations and gateway._preparation_failure is None
    await repo.admit(owner, submit(workspace, "ordinary"), model="test")
    assert await repo.claim_next(owner) is not None


async def test_background_duplicate_bypasses_full_preparation_capacity(runtime, workspace):
    repo, owner, host = runtime
    value = replace(submit(workspace, "duplicate", "job"), lane="background")
    receipt = await repo.admit(owner, value, model="test")
    entered = asyncio.Event()
    preparations = 0

    async def prepare(submission):
        nonlocal preparations
        preparations += 1
        if preparations == 2:
            entered.set()
        await asyncio.Event().wait()

    adapter = QueueGatewayAdapter("local")
    gateway = AgentGateway(
        GatewayScheduler(repo, owner, CodingAssignmentRunner(host)), [adapter],
        IdentityPolicy((IdentityRule("local", "account", "alice", "alice", workspace),)),
        model="test", prepare_background=prepare,
    )
    # Hold the existing task in the waiting queue while exercising channel admissions.
    consumer = asyncio.create_task(gateway._consume(adapter))
    gateway._consumers.append(consumer)
    try:
        for identity in ("pending-a", "pending-b"):
            await adapter.receive_message(
                InboundMessage(identity, "account", "alice", "chat", "/background pending")
            )
        await asyncio.wait_for(entered.wait(), 5)
        await adapter.receive_message(
            InboundMessage("duplicate", "account", "alice", "chat", "/background job")
        )
        await adapter.receive_message(
            InboundMessage("status", "account", "alice", "chat", "/status")
        )

        async def status_recorded():
            return await repo.database.run(lambda c: c.execute(
                "SELECT 1 FROM gateway_inbox WHERE source_message_id='status'"
            ).fetchone())

        await eventually(status_recorded)
        assert preparations == 2 and gateway.rejections == []
        assert (await repo.task(receipt.task_id, principal_id="alice"))["status"] == "queued"
    finally:
        await gateway.shutdown()
    assert not gateway._preparations
    assert await repo.database.run(lambda c: c.execute(
        "SELECT SUM(preparing) FROM gateway_routes"
    ).fetchone()[0]) == 0
