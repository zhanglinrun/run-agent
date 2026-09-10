import asyncio
from dataclasses import replace
from time import time

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import AssistantMessage, TextContent, UserMessage
from run_agent_core.session.contracts import RunOutcome, SessionConflict, StaleRunToken
from run_agent_core.session.entries import MessageEntry
from run_agent_gateway.contracts import (
    AdmissionRejected,
    DuplicateConflict,
    GatewayOwnershipLost,
    RouteIdentity,
    Submission,
)
from run_agent_gateway.outbox import OutboxRepository
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.routing import route_key


@pytest.fixture
async def gateway(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as db:
        clock = [100.0]
        repo = GatewayRepository(db, clock=lambda: clock[0])
        await repo.initialize()
        owner = await repo.acquire_owner("gateway", lease_seconds=30)
        yield repo, owner, clock


def submission(tmp_path, index=0, *, session="one", lane="foreground", principal="alice"):
    return Submission(
        RouteIdentity("adapter", "account", session, subject_id=principal),
        principal,
        f"message-{index}",
        f"task {index}",
        tmp_path / session,
        lane=lane,
        metadata={"sequence": index},
    )


async def admit(repo, owner, value):
    return await repo.admit(owner, value, model="test")


async def test_admission_deduplicates_concurrent_deliveries_and_conflicts(gateway, tmp_path):
    repo, owner, _ = gateway
    message = submission(tmp_path)
    receipts = await asyncio.gather(*(admit(repo, owner, message) for _ in range(8)))
    assert len({receipt.task_id for receipt in receipts}) == 1
    assert sum(not receipt.duplicate for receipt in receipts) == 1
    with pytest.raises(DuplicateConflict):
        await admit(repo, owner, replace(message, content="changed"))
    with pytest.raises(PermissionError):
        await admit(
            repo, owner, replace(message, source_message_id="different", principal_id="bob")
        )
    counts = await repo.database.run(
        lambda connection: {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "gateway_tasks",
                "gateway_inbox",
                "gateway_routes",
                "gateway_outbox",
                "sessions",
            )
        }
    )
    assert set(counts.values()) == {1}
    with pytest.raises(KeyError):
        await repo.task(receipts[0].task_id, principal_id="bob")


def test_structured_route_fields_do_not_collide():
    left = RouteIdentity("a:b", "c", "d", "e", "f")
    right = RouteIdentity("a", "b:c", "d", "e", "f")
    assert route_key(left) != route_key(right)
    assert route_key(left) != route_key(replace(left, thread_id="other"))


@pytest.mark.parametrize("point", ["gateway_task_inserted", "gateway_admitted"])
async def test_failed_admission_publishes_no_receipt_route_or_session(gateway, tmp_path, point):
    repo, owner, _ = gateway

    def fail(name):
        if name == point:
            raise OSError("admission fault")

    repo.fault = fail
    with pytest.raises(OSError):
        await admit(repo, owner, submission(tmp_path))
    counts = await repo.database.run(
        lambda connection: [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "gateway_tasks",
                "gateway_inbox",
                "gateway_routes",
                "gateway_outbox",
                "sessions",
            )
        ]
    )
    assert counts == [0] * 5


async def test_background_cannot_consume_foreground_waiting_reservation(gateway, tmp_path):
    repo, owner, _ = gateway
    repo.limits = replace(
        repo.limits,
        waiting_total=6,
        waiting_foreground_reserved=2,
        waiting_background_reserved=1,
        background_roots_per_principal=20,
    )
    for index in range(4):
        await admit(repo, owner, submission(tmp_path, index, session=str(index), lane="background"))
    with pytest.raises(AdmissionRejected, match="reserved"):
        await admit(repo, owner, submission(tmp_path, 4, session="4", lane="background"))
    for index in (5, 6):
        await admit(repo, owner, submission(tmp_path, index, session=str(index)))
    with pytest.raises(AdmissionRejected):
        await admit(repo, owner, submission(tmp_path, 7))
    original = await admit(repo, owner, submission(tmp_path, 0, session="0", lane="background"))
    assert original.duplicate


async def test_session_limit_background_root_and_payload_limits_are_transactional(
    gateway, tmp_path
):
    repo, owner, _ = gateway
    repo.limits = replace(
        repo.limits, per_session=2, background_roots_per_principal=1, payload_bytes=1024
    )
    await admit(repo, owner, submission(tmp_path, 1))
    await admit(repo, owner, submission(tmp_path, 2))
    with pytest.raises(AdmissionRejected, match="Session"):
        await admit(repo, owner, submission(tmp_path, 3))
    await admit(repo, owner, submission(tmp_path, 4, session="bg1", lane="background"))
    with pytest.raises(AdmissionRejected, match="background root"):
        await admit(repo, owner, submission(tmp_path, 5, session="bg2", lane="background"))
    with pytest.raises(AdmissionRejected, match="byte"):
        await admit(repo, owner, replace(submission(tmp_path, 6), content="x" * 2048))


async def test_ready_sessions_round_robin_and_workspace_waiters_take_no_slot(gateway, tmp_path):
    repo, owner, _ = gateway
    one = await admit(repo, owner, submission(tmp_path, 1))
    two = await admit(repo, owner, submission(tmp_path, 2))
    other = await admit(repo, owner, submission(tmp_path, 3, session="other"))
    shared = await admit(
        repo, owner, replace(submission(tmp_path, 4, session="shared"), workspace=tmp_path / "one")
    )
    first = await repo.claim_next(owner)
    second = await repo.claim_next(owner)
    assert [first.task_id, second.task_id] == [one.task_id, other.task_id]
    assert await repo.claim_next(owner) is None
    await repo.complete(owner, first, status="succeeded", output="done")
    assert await repo.claim_next(owner) is None  # completion is not proof of cleanup
    await repo.release(owner, first)
    third = await repo.claim_next(owner)
    assert third.task_id == shared.task_id  # a never-served session precedes the hot session
    await repo.complete(owner, third, status="succeeded")
    await repo.release(owner, third)
    fourth = await repo.claim_next(owner)
    assert fourth.task_id == two.task_id


async def test_execution_reservations_and_no_early_release_during_cancel(gateway, tmp_path):
    repo, owner, _ = gateway
    repo.limits = replace(repo.limits, background_roots_per_principal=20)
    for index in range(8):
        await admit(repo, owner, submission(tmp_path, index, session=str(index), lane="background"))
    background = [await repo.claim_next(owner) for _ in range(4)]
    assert all(background)
    assert await repo.claim_next(owner) is None
    for index in range(8, 12):
        await admit(repo, owner, submission(tmp_path, index, session=str(index)))
    foreground = [await repo.claim_next(owner) for _ in range(4)]
    assert all(foreground)
    assert await repo.claim_next(owner) is None
    current = foreground[0]
    assert await repo.cancel(owner, current.task_id, principal_id="alice") == "cancelling"
    assert await repo.cancel(owner, current.task_id, principal_id="alice") == "cancelling"
    with pytest.raises(SessionConflict):
        await repo.release(owner, current)
    with pytest.raises(SessionConflict):
        await repo.complete(owner, current, status="succeeded", output="late")
    await repo.complete(owner, current, status="cancelled")
    assert await repo.claim_next(owner) is None
    await repo.release(owner, current)


async def start_coding(repo, owner, assignment):
    token = await repo.sessions.claim(assignment.session_id, owner_id=owner.owner_id, run_id="idle")
    token = await repo.sessions.begin_run(token, branch_id="main", run_id=assignment.run_id)
    message = MessageEntry(id="user", message=UserMessage(content="original task"))
    await repo.sessions.append_entries([message], token=token, expected_head=None)
    return token


@pytest.mark.parametrize("point", ["gateway_task_completed", "gateway_outbox_inserted"])
async def test_final_history_task_outcome_and_delivery_commit_atomically(gateway, tmp_path, point):
    repo, owner, _ = gateway
    await admit(repo, owner, submission(tmp_path))
    assignment = await repo.claim_next(owner)
    token = await start_coding(repo, owner, assignment)
    final = MessageEntry(
        id="answer",
        parent_id="user",
        message=AssistantMessage(content=[TextContent(text="answer")], stop_reason="stop"),
    )
    outcome = RunOutcome(token, "main", "succeeded", "user", (final,))

    def fail(name):
        if name == point:
            raise OSError("completion fault")

    repo.fault = fail
    with pytest.raises(OSError):
        await repo.complete(owner, assignment, status="succeeded", output="answer", outcome=outcome)
    assert (await repo.sessions.get_head(assignment.session_id)).entry_id == "user"
    assert (await repo.task(assignment.task_id, principal_id="alice"))["status"] == "running"
    assert [
        row["kind"]
        for row in await OutboxRepository(repo).for_task(assignment.task_id, principal_id="alice")
    ] == ["accepted"]
    repo.fault = None
    receipt = await repo.complete(
        owner, assignment, status="succeeded", output="answer", outcome=outcome
    )
    assert receipt.head_id == "answer"
    await repo.release(owner, assignment)
    assert (
        await repo.complete(owner, assignment, status="succeeded", output="answer", outcome=outcome)
        == receipt
    )


async def test_stop_revokes_intermediate_history_before_late_result_and_allows_cancel_receipt(
    gateway, tmp_path
):
    repo, owner, _ = gateway
    await admit(repo, owner, submission(tmp_path))
    assignment = await repo.claim_next(owner)
    token = await start_coding(repo, owner, assignment)
    await repo.stop(owner, submission(tmp_path).route, principal_id="alice")
    with pytest.raises(StaleRunToken):
        await repo.sessions.append_entries(
            [MessageEntry(id="late", parent_id="user", message=UserMessage(content="late"))],
            token=token,
            expected_head="user",
        )
    # Keeping the lease while a tool exits does not restore write authority.
    await repo.sessions.renew(token)
    outcome = RunOutcome(token, "main", "cancelled", "user")
    receipt = await repo.complete(owner, assignment, status="cancelled", outcome=outcome)
    assert receipt.status == "cancelled" and receipt.head_id == "user"
    assert await repo.complete(owner, assignment, status="cancelled", outcome=outcome) == receipt


async def test_stop_after_success_keeps_success_and_leaves_background_owned(gateway, tmp_path):
    repo, owner, _ = gateway
    message = submission(tmp_path)
    await admit(repo, owner, message)
    background = await admit(
        repo,
        owner,
        replace(submission(tmp_path, 1, lane="background"), workspace=tmp_path / "background"),
    )
    first = await repo.claim_next(owner)
    await repo.complete(owner, first, status="succeeded", output="finished")
    assert await repo.stop(owner, message.route, principal_id="alice") == {}
    assert (await repo.task(background.task_id, principal_id="alice"))["status"] == "queued"


async def test_restart_recovers_receipts_but_quarantines_unconfirmed_tools(gateway, tmp_path):
    repo, owner, clock = gateway
    message = submission(tmp_path)
    receipt = await admit(repo, owner, message)
    assignment = await repo.claim_next(owner)
    clock[0] += 31
    successor = await repo.acquire_owner("successor")
    assert (await admit(repo, successor, message)).task_id == receipt.task_id
    task = await repo.task(receipt.task_id, principal_id="alice")
    assert task["status"] == "outcome_unknown"
    with pytest.raises(GatewayOwnershipLost):
        await repo.complete(owner, assignment, status="succeeded")
    with pytest.raises(GatewayOwnershipLost):
        await repo.release(owner, assignment)
    await admit(repo, successor, submission(tmp_path, 2))
    assert await repo.claim_next(successor) is None  # same workspace is quarantined


async def test_outbox_retries_same_id_after_restart_and_limits_attempts(gateway, tmp_path):
    repo, owner, clock = gateway
    receipt = await admit(repo, owner, submission(tmp_path))
    outbox = OutboxRepository(repo)
    first = (await outbox.claim(owner))[0]
    assert first.kind == "accepted"
    clock[0] += 31  # external send succeeded but process died before recording the receipt
    successor = await repo.acquire_owner("next")
    repeated = (await outbox.claim(successor))[0]
    assert repeated.delivery_id == first.delivery_id and repeated.attempt == 2
    with pytest.raises(GatewayOwnershipLost):
        await outbox.acknowledge(owner, first, {"id": "old"})
    await outbox.fail(successor, repeated, "temporary channel failure", max_attempts=3)
    assert await outbox.claim(successor) == []
    clock[0] += 2
    last = (await outbox.claim(successor))[0]
    await outbox.fail(successor, last, "still failing", max_attempts=3)
    rows = await outbox.for_task(receipt.task_id, principal_id="alice")
    assert rows[0]["status"] == "failed" and rows[0]["attempts"] == 3


async def test_result_delivery_survives_later_execution_and_stays_after_acceptance(
    gateway, tmp_path
):
    repo, owner, _ = gateway
    await admit(repo, owner, submission(tmp_path))
    assignment = await repo.claim_next(owner)
    await repo.complete(owner, assignment, status="succeeded", output="first result")
    await repo.release(owner, assignment)
    outbox = OutboxRepository(repo)
    accepted = await outbox.claim(owner)
    assert [delivery.kind for delivery in accepted] == ["accepted"]
    await outbox.acknowledge(owner, accepted[0], {"channel_message_id": "one"})
    await admit(repo, owner, submission(tmp_path, 1))
    assert (await repo.claim_next(owner)).run_id != assignment.run_id
    deliveries = await outbox.claim(owner)
    result = next(delivery for delivery in deliveries if delivery.kind == "result")
    assert result.content["output"] == "first result"
    await outbox.acknowledge(owner, result, {"channel_message_id": "two"})
    await outbox.acknowledge(owner, result, {"channel_message_id": "two"})


async def test_actual_database_reopen_keeps_admission_identity_and_pending_delivery(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    message = submission(tmp_path)
    async with await SqliteDatabase.open(path) as db:
        repo = GatewayRepository(db)
        await repo.initialize()
        owner = await repo.acquire_owner("first")
        admitted = await admit(repo, owner, message)
        await repo.stop_accepting(owner)
        await repo.release_owner(owner)
    async with await SqliteDatabase.open(path) as db:
        repo = GatewayRepository(db)
        await repo.initialize()
        owner = await repo.acquire_owner("second")
        repeated = await admit(repo, owner, message)
        assert repeated.duplicate and repeated.task_id == admitted.task_id
        assignment = await repo.claim_next(owner)
        assert assignment.task_id == admitted.task_id
        deliveries = await OutboxRepository(repo).claim(owner)
        assert len(deliveries) == 1 and deliveries[0].content["task_id"] == admitted.task_id


async def test_coding_application_uses_assigned_run_and_atomic_gateway_committer(gateway, tmp_path):
    repo, owner, _ = gateway
    (tmp_path / "one").mkdir()
    await admit(repo, owner, submission(tmp_path))
    assignment = await repo.claim_next(owner)
    opts = replace(options(tmp_path), resume=assignment.session_id)
    manager = SessionManager(
        opts.paths, database=repo.database, principal_id="alice", owner_id=owner.owner_id
    )
    # Coding uses wall time for its leases, so the composed commit must do so too.
    repo.clock = time
    repo.sessions.clock = time
    await repo.database.run(
        lambda connection: connection.execute(
            "UPDATE gateway_owner SET expires_at=?", (time() + 30,)
        ),
        write=True,
    )
    try:
        async with await CodingApplication.open(
            opts,
            manager=manager,
            provider=ReplyProvider(),
            committer=repo.committer(owner, assignment),
        ) as app:
            events = [
                event async for event in app.prompt(assignment.content, run_id=assignment.run_id)
            ]
            receipt = events[-1]
            assert receipt.run_id == assignment.run_id and receipt.status == "succeeded"
            task = await repo.task(assignment.task_id, principal_id="alice")
            assert task["status"] == "succeeded" and task["output"] == "reply: task 0"
            entries = (await app.session.storage.read_entries()).entries
            assert entries[-1].id == receipt.head_id
            assert [
                row["kind"]
                for row in await OutboxRepository(repo).for_task(
                    assignment.task_id, principal_id="alice"
                )
            ] == ["accepted", "result"]
        await repo.release(owner, assignment)
    finally:
        await manager.aclose()


async def test_delivery_capacity_reserves_room_for_already_admitted_results(gateway, tmp_path):
    repo, owner, _ = gateway
    repo.limits = replace(repo.limits, outbox_pending=4)
    await admit(repo, owner, submission(tmp_path))
    await admit(repo, owner, submission(tmp_path, 1, session="other"))
    with pytest.raises(AdmissionRejected, match="Delivery backlog"):
        await admit(repo, owner, submission(tmp_path, 2, session="third"))
    for _ in range(2):
        assignment = await repo.claim_next(owner)
        await repo.complete(owner, assignment, status="succeeded", output="done")
        await repo.release(owner, assignment)
    assert (
        await repo.database.run(
            lambda connection: connection.execute("SELECT COUNT(*) FROM gateway_outbox").fetchone()[
                0
            ]
        )
        == 4
    )
