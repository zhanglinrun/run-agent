import asyncio
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import AssistantMessage, UserMessage
from run_agent_core.session.entries import MessageEntry
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.contracts import AdmissionRejected, GatewayLimits, RouteIdentity, Submission
from run_agent_gateway.controller import SessionController
from run_agent_gateway.gateway import (
    AgentGateway,
    BoundedIngress,
    InboundMessage,
    QueueGatewayAdapter,
)
from run_agent_gateway.identity import IdentityPolicy, IdentityRule
from run_agent_gateway.ownership import GatewayProcessLock
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.scheduler import GatewayScheduler


@pytest.fixture
async def runtime(tmp_path):
    opts = options(tmp_path)
    async with await SqliteDatabase.open(opts.paths.database_path) as db:
        repo = GatewayRepository(db)
        await repo.initialize()
        owner = await repo.acquire_owner("host")
        host = GatewayCodingRuntime(repo, owner, opts, provider_factory=lambda _: ReplyProvider())
        yield repo, owner, host


def submit(tmp_path, message="first", text="hello", chat="chat"):
    return Submission(
        RouteIdentity("local", "account", chat, subject_id="alice"),
        "alice",
        message,
        text,
        tmp_path,
    )


async def eventually(check, *, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.005)


async def released(repo, task_id):
    return await repo.database.run(
        lambda c: c.execute(
            "SELECT EXISTS(SELECT 1 FROM gateway_attempts WHERE task_id=? AND released=1)",
            (task_id,),
        ).fetchone()[0]
    )


async def test_actual_dispatcher_runs_and_reopens_one_session_fifo(runtime, tmp_path):
    repo, owner, host = runtime
    runner = CodingAssignmentRunner(host)
    scheduler = GatewayScheduler(repo, owner, runner)
    receipts = [
        await repo.admit(owner, submit(tmp_path, str(i), str(i)), model="test") for i in range(5)
    ]
    await scheduler.start()
    try:
        await eventually(lambda: released(repo, receipts[-1].task_id))
        assert not scheduler.errors and scheduler.failure is None
        assert runner.applications == {}
        history = (await repo.sessions.read_entries(receipts[0].session_id)).entries
        assert [
            entry.message.text
            for entry in history
            if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage)
        ] == [str(i) for i in range(5)]
        rows = await repo.database.run(
            lambda c: c.execute(
                "SELECT t.status,e.status,o.status FROM gateway_tasks t JOIN executions e "
                "ON t.run_id=e.run_id JOIN gateway_outbox o ON t.task_id=o.task_id AND o.kind='result'"
            ).fetchall()
        )
        assert [tuple(row) for row in rows] == [("succeeded", "succeeded", "pending")] * 5
    finally:
        await scheduler.shutdown()
        await repo.release_owner(owner)


async def test_real_prompt_stop_discards_late_answer_and_retains_slot_until_cleanup(
    runtime, tmp_path
):
    repo, owner, host = runtime
    started, cancelling, allow_exit = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await allow_exit.wait()
                async for event in super().stream_response(**kwargs):
                    yield event

    host.provider_factory = lambda _: DelayedProvider()
    runner = CodingAssignmentRunner(host)
    scheduler = GatewayScheduler(repo, owner, runner)
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await repo.cancel(owner, receipt.task_id, principal_id="alice") == "cancelling"
        scheduler.signal_cancel(receipt.task_id)
        await asyncio.wait_for(cancelling.wait(), 5)
        assert not await released(repo, receipt.task_id)
        assert scheduler.active_count == 1
        allow_exit.set()
        await eventually(lambda: released(repo, receipt.task_id))
        assert not scheduler.errors
        state = await repo.task(receipt.task_id, principal_id="alice")
        assert state["status"] == "cancelled" and state["output"] == ""
        entries = (await repo.sessions.read_entries(receipt.session_id)).entries
        assert not any(
            isinstance(e, MessageEntry) and isinstance(e.message, AssistantMessage) for e in entries
        )
        # A new assignment opens from the authoritative history after cancellation.
        host.provider_factory = lambda _: ReplyProvider()
        next_receipt = await repo.admit(owner, submit(tmp_path, "next", "next"), model="test")
        scheduler.wake()
        await eventually(lambda: released(repo, next_receipt.task_id))
        assert (await repo.task(next_receipt.task_id, principal_id="alice"))[
            "status"
        ] == "succeeded"
    finally:
        allow_exit.set()
        await scheduler.shutdown()
        await repo.release_owner(owner)


async def test_new_binding_waits_for_real_release_and_deduplicates(runtime, tmp_path):
    repo, owner, _ = runtime
    controller = SessionController(repo)
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    command = submit(tmp_path, "new-command", "/new")
    reply = await controller.handle(owner, command, model="test")
    assert reply["status"] == "stopping"
    assert (await controller.handle(owner, command, model="test"))["duplicate"]
    await controller.reconcile(owner)
    with pytest.raises(AdmissionRejected, match="stopping"):
        await repo.admit(owner, submit(tmp_path, "next"), model="test")
    await repo.complete(owner, assignment, status="cancelled")
    await controller.reconcile(owner)
    with pytest.raises(AdmissionRejected, match="stopping"):
        await repo.admit(owner, submit(tmp_path, "next"), model="test")
    await repo.release(owner, assignment)
    await controller.reconcile(owner)
    fresh = await repo.admit(owner, submit(tmp_path, "next"), model="test")
    assert fresh.session_id != receipt.session_id and fresh.conversation_epoch == 2
    assert (await controller.handle(owner, command, model="test"))["control_id"] == reply[
        "control_id"
    ]
    count = await repo.database.run(
        lambda c: c.execute(
            "SELECT COUNT(*) FROM gateway_outbox WHERE control_id=?", (reply["control_id"],)
        ).fetchone()[0]
    )
    assert count == 2


async def test_adapter_receipts_and_result_flow_through_durable_outbox(runtime, tmp_path):
    repo, owner, host = runtime
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    adapter = QueueGatewayAdapter("local")
    policy = IdentityPolicy((IdentityRule("local", "account", "alice", "alice", tmp_path),))
    gateway = AgentGateway(scheduler, [adapter], policy, model="test", send_timeout=1)
    await gateway.start()
    try:
        message = InboundMessage("message-1", "account", "alice", "chat", "hello")
        await adapter.receive_message(message)
        accepted = await asyncio.wait_for(adapter.next_sent(), 5)
        result = await asyncio.wait_for(adapter.next_sent(), 5)
        assert accepted.content["status"] == "accepted"
        assert result.content["output"] == "reply: hello"
        assert accepted.task_id == result.task_id
        await adapter.receive_message(message)
        await adapter.receive_message(replace(message, source_message_id="status", text="/status"))
        status = await asyncio.wait_for(adapter.next_sent(), 5)
        assert status.content["tasks"][0]["task_id"] == accepted.task_id
        await adapter.receive_message(
            replace(message, source_message_id="bad", sender_id="mallory")
        )
        rejected = await asyncio.wait_for(adapter.next_sent(), 5)
        assert rejected.content["status"] == "rejected"
        assert (
            await repo.database.run(
                lambda c: c.execute("SELECT COUNT(*) FROM gateway_tasks").fetchone()[0]
            )
            == 1
        )
    finally:
        await gateway.shutdown()


async def test_bounded_ingress_reserves_control_space():
    ingress = BoundedIngress(ordinary_capacity=1, control_capacity=1)
    message = InboundMessage("1", "account", "alice", "chat", "work")
    ingress.put(message)
    with pytest.raises(AdmissionRejected):
        ingress.put(replace(message, source_message_id="2"))
    ingress.put(replace(message, source_message_id="status", text="/status"))
    ingress.close()
    assert [item.text async for item in ingress.messages()] == ["/status", "work"]


@pytest.mark.parametrize("command", ["/new", "/stop", "/steer correction"])
async def test_destructive_control_does_not_overtake_earlier_chat_inputs(command):
    ingress = BoundedIngress()
    message = InboundMessage("before", "account", "alice", "chat", "before")
    ingress.put(replace(message, source_message_id="other", chat_id="other", text="other"))
    ingress.put(message)
    ingress.put(replace(message, source_message_id="control", text=command))
    ingress.put(replace(message, source_message_id="after", text="after"))
    ingress.close()
    assert [item.text async for item in ingress.messages()] == ["before", command, "other", "after"]


async def test_cleanup_failure_quarantines_committed_task(runtime, tmp_path):
    repo, owner, _ = runtime

    class BadCleanup:
        async def run(self, assignment, cancellation):
            await repo.complete(owner, assignment, status="succeeded", output="done")
            raise RuntimeError("owned child process did not exit")

    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    scheduler = GatewayScheduler(repo, owner, BadCleanup())
    await scheduler.start()
    try:

        async def contained():
            return await repo.database.run(
                lambda c: c.execute(
                    "SELECT 1 FROM gateway_workspaces WHERE status='quarantined'"
                ).fetchone()
            )

        await eventually(contained)
        assert not await released(repo, receipt.task_id)
        assert (await repo.task(receipt.task_id, principal_id="alice"))["status"] == "succeeded"
    finally:
        await scheduler.shutdown()


def test_process_lock_excludes_a_second_host_and_releases(tmp_path):
    left, right = (
        GatewayProcessLock(tmp_path / "gateway.lock"),
        GatewayProcessLock(tmp_path / "gateway.lock"),
    )
    left.acquire()
    try:
        with pytest.raises(OSError):
            right.acquire()
    finally:
        left.close()
    right.acquire()
    right.close()


async def test_many_queued_tasks_allocate_only_running_coroutines(runtime, tmp_path):
    repo, owner, _ = runtime
    repo.limits = replace(
        GatewayLimits(),
        running_total=4,
        running_foreground_reserved=2,
        running_background_reserved=1,
    )
    gate = asyncio.Event()

    class Runner:
        async def run(self, assignment, cancellation):
            await gate.wait()
            await repo.complete(owner, assignment, status="succeeded")

    receipts = []
    for i in range(100):
        value = submit(tmp_path / str(i), str(i), str(i), chat=str(i))
        receipts.append(await repo.admit(owner, value, model="test"))
    scheduler = GatewayScheduler(repo, owner, Runner())
    await scheduler.start()
    try:

        async def full():
            return scheduler.active_count == 3

        await eventually(full)
        assert len([t for t in asyncio.all_tasks() if t.get_name().startswith("gateway-run:")]) == 3
        assert (
            await repo.database.run(
                lambda c: c.execute(
                    "SELECT COUNT(*) FROM gateway_tasks WHERE status='queued'"
                ).fetchone()[0]
            )
            == 97
        )
        gate.set()
        await eventually(lambda: released(repo, receipts[-1].task_id))
        assert not scheduler.errors
    finally:
        gate.set()
        await scheduler.shutdown()


async def test_stop_final_notification_waits_for_cleanup(runtime, tmp_path):
    repo, owner, _ = runtime
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    controller = SessionController(repo)
    reply = await controller.handle(owner, submit(tmp_path, "stop", "/stop"), model="test")
    assert reply["status"] == "stopping"
    await repo.complete(owner, assignment, status="cancelled")
    await controller.reconcile(owner)
    assert (
        await repo.database.run(
            lambda c: c.execute(
                "SELECT state FROM gateway_controls WHERE control_id=?", (reply["control_id"],)
            ).fetchone()[0]
        )
        == "waiting"
    )
    await repo.release(owner, assignment)
    await controller.reconcile(owner)
    assert (
        await repo.database.run(
            lambda c: c.execute(
                "SELECT json_extract(content_json,'$.status') FROM gateway_outbox "
                "WHERE control_id=? AND kind='control'",
                (reply["control_id"],),
            ).fetchone()[0]
        )
        == "stopped"
    )
    assert (await repo.task(receipt.task_id, principal_id="alice"))["status"] == "cancelled"


async def test_route_cannot_lease_a_workspace_other_than_its_coding_session(runtime, tmp_path):
    repo, owner, _ = runtime
    await repo.admit(owner, submit(tmp_path), model="test")
    with pytest.raises(AdmissionRejected, match="different workspace"):
        await repo.admit(owner, submit(tmp_path / "other", "next"), model="test")


async def test_channel_timeout_retries_the_same_delivery_id(runtime, tmp_path):
    repo, owner, host = runtime
    policy = IdentityPolicy((IdentityRule("local", "account", "alice", "alice", tmp_path),))

    class LostReceipt(QueueGatewayAdapter):
        lost = False
        attempts = []

        async def send(self, delivery):
            self.attempts.append(delivery.delivery_id)
            receipt = await super().send(delivery)
            if not self.lost:
                self.lost = True
                raise TimeoutError("channel accepted but receipt was lost")
            return receipt

    adapter = LostReceipt("local")
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    gateway = AgentGateway(scheduler, [adapter], policy, model="test", send_timeout=0.2)
    await gateway.start()
    try:
        await adapter.receive_message(InboundMessage("1", "account", "alice", "chat", "hello"))
        accepted = await asyncio.wait_for(adapter.next_sent(), 5)
        result = await asyncio.wait_for(adapter.next_sent(), 5)
        assert result.kind == "result"
        assert adapter.attempts.count(accepted.delivery_id) == 2
        assert adapter._outgoing.empty()
    finally:
        await gateway.shutdown()
