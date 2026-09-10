import asyncio
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider
from tests.redesign.test_gateway_runtime import eventually, released, runtime, submit

from run_agent_coding.host.inputs import InputBoundary
from run_agent_core.messages import AssistantMessage, CustomMessage, UserMessage
from run_agent_core.provider_events import AssistantDoneEvent, AssistantErrorEvent
from run_agent_core.session.contracts import RunOutcome
from run_agent_core.session.entries import MessageEntry
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.contracts import AdmissionRejected, DuplicateConflict
from run_agent_gateway.controller import SessionController
from run_agent_gateway.gateway import AgentGateway, InboundMessage, QueueGatewayAdapter
from run_agent_gateway.identity import IdentityPolicy, IdentityRule
from run_agent_gateway.scheduler import GatewayScheduler

__all__ = ["runtime"]


async def steer(repo, owner, path, text="correction", message="correction"):
    return await SessionController(repo).handle(
        owner, submit(path, message, f"/steer {text}"), model="test"
    )


async def test_busy_steer_is_consumed_once_at_next_boundary_with_durable_history(runtime, tmp_path):
    repo, owner, host = runtime
    started, finish = asyncio.Event(), asyncio.Event()
    observed = []

    class ControlledProvider(ReplyProvider):
        async def stream_response(self, *, messages, **kwargs):
            if any(m.text.startswith("Create a concise session name") for m in messages):
                async for event in super().stream_response(messages=messages, **kwargs):
                    yield event
                return
            observed.append(
                [m.text for m in messages if isinstance(m, (UserMessage, CustomMessage))]
            )
            if len(observed) == 1:
                started.set()
                await finish.wait()
            async for event in super().stream_response(messages=messages, **kwargs):
                yield event

    host.provider_factory = lambda _: ControlledProvider()
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    receipt = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), 5)
        first = await steer(repo, owner, tmp_path)
        second = await steer(repo, owner, tmp_path, "another correction", "second")
        assert (await steer(repo, owner, tmp_path))["duplicate"]
        state = await repo.task(first["task_id"], principal_id="alice")
        assert state["status"] == "steering"
        assert (
            state["target_run_id"]
            == (await repo.task(receipt.task_id, principal_id="alice"))["run_id"]
        )
        assert len(observed) == 1
        finish.set()
        await eventually(lambda: released(repo, receipt.task_id))
        assert not scheduler.errors and scheduler.failure is None
        assert observed == [
            ["hello"],
            ["hello", "correction"],
            ["hello", "correction", "another correction"],
        ]
        for response in (first, second):
            state = await repo.task(response["task_id"], principal_id="alice")
            assert state["status"] == "consumed" and state["attempt"] == 0
            entry = await repo.database.run(
                lambda c, state=state: c.execute(
                    "SELECT run_id FROM entries WHERE entry_id=?", (state["consumed_entry_id"],)
                ).fetchone()
            )
            assert entry["run_id"] == state["target_run_id"]
        history = (await repo.sessions.read_entries(receipt.session_id)).entries
        corrections = [
            e
            for e in history
            if isinstance(e, MessageEntry) and isinstance(e.message, CustomMessage)
        ]
        assert [e.message.text for e in corrections] == ["correction", "another correction"]
        assert (
            len(
                [
                    e
                    for e in history
                    if isinstance(e, MessageEntry) and isinstance(e.message, AssistantMessage)
                ]
            )
            == 3
        )
    finally:
        finish.set()
        await scheduler.shutdown()


async def test_steer_transaction_rolls_back_history_receipt_and_state(runtime, tmp_path):
    repo, owner, host = runtime
    await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    app = await host.open(assignment)
    try:
        token = await app.session.storage.begin_run(assignment.run_id)
        initial = (await app.session.storage.get_head()).entry_id
        response = await steer(repo, owner, tmp_path)
        boundary = InputBoundary(token, app.session.storage.branch_id, initial, ())

        def fail(point):
            if point == "gateway_steering_consumed":
                raise RuntimeError("transaction fault")

        repo.fault = fail
        with pytest.raises(RuntimeError, match="transaction fault"):
            await repo.input_source(owner, assignment)(boundary)
        assert (await app.session.storage.get_head()).entry_id == initial
        assert (await repo.task(response["task_id"], principal_id="alice"))["status"] == "steering"
        assert (
            await repo.database.run(
                lambda c: c.execute(
                    "SELECT COUNT(*) FROM gateway_outbox WHERE task_id=? AND kind='result'",
                    (response["task_id"],),
                ).fetchone()[0]
            )
            == 0
        )
        repo.fault = None
        batch = await repo.input_source(owner, assignment)(boundary)
        assert batch is not None and len(batch.entries) == 1
        assert (
            await repo.input_source(owner, assignment)(
                replace(boundary, expected_head=batch.receipt.head_id)
            )
            is None
        )
        await app.session.storage.complete_run(
            RunOutcome(
                token=token,
                branch_id=boundary.branch_id,
                expected_head=batch.receipt.head_id,
                entries=(),
                status="succeeded",
            )
        )
    finally:
        repo.fault = None
        await app.aclose()
        await app.manager.aclose()


async def test_steer_end_race_requeues_same_task_in_original_order_atomically(runtime, tmp_path):
    repo, owner, _ = runtime
    first = await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    correction = await steer(repo, owner, tmp_path)
    later = await repo.admit(owner, submit(tmp_path, "later", "later"), model="test")

    def fail(point):
        if point == "gateway_steering_requeued":
            raise RuntimeError("conversion fault")

    repo.fault = fail
    with pytest.raises(RuntimeError, match="conversion fault"):
        await repo.complete(owner, assignment, status="succeeded")
    assert (await repo.task(first.task_id, principal_id="alice"))["status"] == "running"
    assert (await repo.task(correction["task_id"], principal_id="alice"))["status"] == "steering"
    repo.fault = None
    await repo.complete(owner, assignment, status="succeeded")
    assert (await repo.task(correction["task_id"], principal_id="alice"))["status"] == "queued"
    assert (await steer(repo, owner, tmp_path))["task_id"] == correction["task_id"]
    await repo.release(owner, assignment)
    fallback = await repo.claim_next(owner)
    assert fallback.task_id == correction["task_id"] and fallback.session_id == first.session_id
    await repo.complete(owner, fallback, status="succeeded")
    await repo.release(owner, fallback)
    assert (await repo.claim_next(owner)).task_id == later.task_id


async def test_idle_steer_uses_normal_admission_and_duplicate_conflicts(runtime, tmp_path):
    repo, owner, _ = runtime
    response = await steer(repo, owner, tmp_path)
    state = await repo.task(response["task_id"], principal_id="alice")
    assert state["status"] == "queued" and state["target_run_id"] is None
    with pytest.raises(DuplicateConflict):
        await steer(repo, owner, tmp_path, "changed")
    with pytest.raises(ValueError, match="Usage"):
        await steer(repo, owner, tmp_path, "", "empty")


async def test_steering_reserves_fallback_capacity_and_stop_cancels_it(runtime, tmp_path):
    repo, owner, _ = runtime
    repo.limits = replace(repo.limits, per_session=1)
    first = await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    response = await steer(repo, owner, tmp_path)
    with pytest.raises(AdmissionRejected, match="waiting limit"):
        await steer(repo, owner, tmp_path, "overflow", "overflow")
    with pytest.raises(AdmissionRejected, match="waiting limit"):
        await repo.admit(owner, submit(tmp_path, "overflow"), model="test")
    await SessionController(repo).handle(owner, submit(tmp_path, "stop", "/stop"), model="test")
    assert (await repo.task(response["task_id"], principal_id="alice"))["status"] == "cancelled"
    assert (await repo.task(first.task_id, principal_id="alice"))["status"] == "cancelling"
    await repo.complete(owner, assignment, status="cancelled")
    assert (await repo.task(response["task_id"], principal_id="alice"))["status"] == "cancelled"


async def test_restart_requeues_unconsumed_input_but_keeps_workspace_quarantined(runtime, tmp_path):
    repo, owner, _ = runtime
    now = [100.0]
    repo.clock = lambda: now[0]
    await repo.admit(owner, submit(tmp_path), model="test")
    assignment = await repo.claim_next(owner)
    response = await steer(repo, owner, tmp_path)
    await repo.renew(owner, lease_seconds=1)
    now[0] += 2
    fresh = await repo.acquire_owner("new-owner")
    state = await repo.task(response["task_id"], principal_id="alice")
    assert state["status"] == "queued" and state["target_run_id"] == assignment.run_id
    assert state["workspace_status"] == "quarantined"
    assert await repo.claim_next(fresh) is None
    assert (await steer(repo, fresh, tmp_path))["duplicate"]


async def test_adapter_delivers_accepted_before_consumed_and_exposes_status(runtime, tmp_path):
    repo, owner, host = runtime
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    class DelayedProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            nonlocal calls
            if any(m.text.startswith("Create a concise session name") for m in kwargs["messages"]):
                async for event in super().stream_response(**kwargs):
                    yield event
                return
            calls += 1
            if calls == 1:
                started.set()
                await finish.wait()
            yield AssistantDoneEvent(
                reason="stop", message=AssistantMessage(content="done", stop_reason="stop")
            )

    host.provider_factory = lambda _: DelayedProvider()
    adapter = QueueGatewayAdapter("local")
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    gateway = AgentGateway(
        scheduler,
        [adapter],
        IdentityPolicy((IdentityRule("local", "account", "alice", "alice", tmp_path),)),
        model="test",
    )
    await gateway.start()
    try:
        await adapter.receive_message(InboundMessage("first", "account", "alice", "chat", "hello"))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(adapter.next_sent(), 5)
        await adapter.receive_message(
            InboundMessage("steer", "account", "alice", "chat", "/steer correction")
        )
        accepted = await asyncio.wait_for(adapter.next_sent(), 5)
        assert accepted.content["mode"] == "steer" and accepted.content["status"] == "accepted"
        finish.set()
        deliveries = [await asyncio.wait_for(adapter.next_sent(), 5) for _ in range(2)]
        consumed = next(d for d in deliveries if d.task_id == accepted.task_id)
        assert consumed.content["status"] == "consumed" and calls == 2
        assert not gateway.rejections and not scheduler.errors
    finally:
        finish.set()
        await gateway.shutdown()


async def test_model_error_requeues_unconsumed_steer_for_real_runner(runtime, tmp_path):
    repo, owner, host = runtime
    started, finish = asyncio.Event(), asyncio.Event()

    class ErrorProvider(ReplyProvider):
        async def stream_response(self, **kwargs):
            if any(m.text.startswith("Create a concise session name") for m in kwargs["messages"]):
                async for event in super().stream_response(**kwargs):
                    yield event
                return
            started.set()
            await finish.wait()
            yield AssistantErrorEvent(
                reason="error",
                error=AssistantMessage(
                    content=[],
                    model="test",
                    provider="test",
                    stop_reason="error",
                    error_message="offline error",
                ),
            )

    host.provider_factory = lambda _: ErrorProvider()
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    first = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), 5)
        correction = await steer(repo, owner, tmp_path)
        host.provider_factory = lambda _: ReplyProvider()
        finish.set()
        await eventually(lambda: released(repo, correction["task_id"]))
        assert not scheduler.errors and scheduler.failure is None
        assert (await repo.task(first.task_id, principal_id="alice"))["status"] == "failed"
        state = await repo.task(correction["task_id"], principal_id="alice")
        assert state["status"] == "succeeded" and state["consumed_entry_id"] is None
        assert state["output"] == "reply: correction"
        history = (await repo.sessions.read_entries(first.session_id)).entries
        assert (
            len(
                [
                    e
                    for e in history
                    if isinstance(e, MessageEntry) and e.message.text == "correction"
                ]
            )
            == 1
        )
    finally:
        finish.set()
        await scheduler.shutdown()


async def test_cancel_during_input_commit_retains_consumed_history_without_second_task(
    runtime, tmp_path
):
    repo, owner, host = runtime
    commit_finished, return_commit = asyncio.Event(), asyncio.Event()
    original_source = repo.input_source
    correction = None

    def delayed_source(owner, assignment):
        read = original_source(owner, assignment)

        async def receive(boundary):
            nonlocal correction
            if correction is None:
                correction = await steer(repo, owner, tmp_path)
            result = await read(boundary)
            if result is not None:
                commit_finished.set()
                await return_commit.wait()
            return result

        return receive

    repo.input_source = delayed_source
    scheduler = GatewayScheduler(repo, owner, CodingAssignmentRunner(host))
    first = await repo.admit(owner, submit(tmp_path), model="test")
    await scheduler.start()
    try:
        await asyncio.wait_for(commit_finished.wait(), 5)
        await repo.cancel(owner, first.task_id, principal_id="alice")
        scheduler.signal_cancel(first.task_id)
        assert not await released(repo, first.task_id)
        return_commit.set()
        await eventually(lambda: released(repo, first.task_id))
        assert not scheduler.errors and scheduler.failure is None
        assert (await repo.task(first.task_id, principal_id="alice"))["status"] == "cancelled"
        state = await repo.task(correction["task_id"], principal_id="alice")
        assert state["status"] == "consumed" and state["attempt"] == 0
        entries = (await repo.sessions.read_entries(first.session_id)).entries
        assert state["consumed_entry_id"] in {e.id for e in entries}
        assert not any(
            isinstance(e, MessageEntry) and isinstance(e.message, AssistantMessage) for e in entries
        )
    finally:
        return_commit.set()
        await scheduler.shutdown()
