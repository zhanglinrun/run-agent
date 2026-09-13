"""Lease, ledger, heartbeat and stall detection: the mechanisms behind a trustworthy gateway."""

import asyncio
import subprocess
import sys
import time

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_gateway_runner import BlockingProvider, FakeAdapter, config

from run_agent_gateway.heartbeat import HeartbeatScheduler, HeartbeatStore
from run_agent_gateway.lease import SessionTurnLeaseRegistry, TurnLeaseTimeoutError
from run_agent_gateway.ledger import RECOVERED_MARKER, DeliveryLedger
from run_agent_gateway.platforms.base import MessageEvent
from run_agent_gateway.run import GatewayRunner
from run_agent_gateway.session import SessionSource
from run_agent_gateway.stall import StallMonitor, format_stall_notice

# --- turn lease ----------------------------------------------------------------------


async def test_lease_serializes_holders_and_rejects_stale_release():
    registry = SessionTurnLeaseRegistry(max_entries=2)
    first = await registry.acquire("s1", "chat-a", timeout=1)
    assert registry.is_held("s1") and registry.holder("s1") == "chat-a"
    with pytest.raises(TurnLeaseTimeoutError, match="held by chat-a"):
        await registry.acquire("s1", "chat-b", timeout=0.05)
    waiter = asyncio.create_task(registry.acquire("s1", "chat-b", timeout=2))
    await asyncio.sleep(0.02)
    assert not waiter.done()
    assert await registry.release(first)
    second = await waiter
    assert second.generation == first.generation + 1
    # A stale token (the first holder releasing again) cannot free the new holder.
    assert not await registry.release(first)
    assert registry.is_held("s1")
    assert await registry.release(second)
    # Idle leases are evicted at the cap; held ones never are.
    held = await registry.acquire("s2", "x", timeout=1)
    await registry.acquire("s3", "y", timeout=1)
    assert registry.is_held("s2") and "s1" not in registry._leases
    await registry.release(held)


async def test_two_chats_on_one_session_take_turns(tmp_path):
    adapter = FakeAdapter()
    blocking = BlockingProvider()
    gateway = GatewayRunner(
        config(turn_lease_timeout_seconds=5),
        options(tmp_path),
        adapter,
        provider_factory=lambda: blocking,
    )
    await gateway.start()
    try:
        thread = SessionSource("fake", "chat", "group", user_id="alice", thread_id="t1")
        plain = SessionSource("fake", "chat", "group", user_id="alice")
        # Force both chat keys onto the same coding session.
        shared = gateway.store.get_or_create(adapter.session_key_for(thread), thread)
        other = gateway.store.get_or_create(adapter.session_key_for(plain), plain)
        other.session_id = shared.session_id
        await adapter.handle_message(MessageEvent("one", thread, message_id="m1"))
        await asyncio.wait_for(blocking.started.wait(), 10)
        await adapter.handle_message(MessageEvent("two", plain, message_id="m2"))
        await asyncio.sleep(0.1)
        assert adapter.is_busy(adapter.session_key_for(plain))
        assert gateway.leases.holder(shared.session_id) == adapter.session_key_for(thread)
        blocking.release.set()
        await adapter.wait_idle()
        assert [reply_to for _, _, reply_to in adapter.sent] == ["m1", "m2"]
    finally:
        await gateway.stop()


async def test_lease_timeout_tells_the_user_to_resend(tmp_path):
    adapter = FakeAdapter()
    blocking = BlockingProvider()
    gateway = GatewayRunner(
        config(turn_lease_timeout_seconds=1),
        options(tmp_path),
        adapter,
        provider_factory=lambda: blocking,
    )
    await gateway.start()
    try:
        a = SessionSource("fake", "chat", "dm", user_id="alice")
        b = SessionSource("fake", "chat", "dm", user_id="alice", thread_id="t")
        shared = gateway.store.get_or_create(adapter.session_key_for(a), a)
        gateway.store.get_or_create(adapter.session_key_for(b), b).session_id = shared.session_id
        await adapter.handle_message(MessageEvent("slow", a, message_id="m1"))
        await asyncio.wait_for(blocking.started.wait(), 10)
        await adapter.handle_message(MessageEvent("now", b, message_id="m2"))
        await asyncio.sleep(1.5)
        assert any("另一个聊天占用" in text for _, text, _ in adapter.sent)
        blocking.release.set()
        await adapter.wait_idle()
    finally:
        await gateway.stop()


# --- delivery ledger -----------------------------------------------------------------


def test_ledger_checkpoints_and_recovers_only_dead_owners(tmp_path):
    ledger = DeliveryLedger(tmp_path / "d.sqlite3")
    one = ledger.record("k", "chat", "hello", reply_to="m1")
    ledger.mark_attempting(one.obligation_id)
    two = ledger.record("k", "chat", "bye")
    ledger.mark_delivered(two.obligation_id)
    duplicate = ledger.record("k", "chat", "bye")
    assert duplicate.state == "delivered"
    # Our own rows are alive, so nothing is recoverable from this process.
    assert ledger.sweep_recoverable() == []
    assert {row.state for row in ledger.rows()} == {"attempting", "delivered"}


async def test_gateway_ledger_tracks_each_reply_chunk(tmp_path):
    adapter = FakeAdapter()
    adapter.max_message_length = 8
    ledger = DeliveryLedger(tmp_path / "chunks.sqlite3")
    adapter.ledger = ledger
    result = await adapter.deliver_reply("session", "chat", "one two three four", reply_to="input")
    assert result.success
    rows = sorted(ledger.rows(limit=20), key=lambda row: row.chunk_index)
    assert len(rows) > 1
    assert all(row.state == "delivered" for row in rows)
    assert [row.chunk_index for row in rows] == list(range(len(rows)))
    assert {row.chunk_count for row in rows} == {len(rows)}
    assert len(adapter.sent) == len(rows)
    repeated = await adapter.deliver_reply(
        "session", "chat", "one two three four", reply_to="input"
    )
    assert repeated.success
    assert len(adapter.sent) == len(rows)


def test_ledger_redelivers_after_a_real_process_crash(tmp_path):
    path = tmp_path / "d.sqlite3"
    code = f"""
from pathlib import Path
from run_agent_gateway.ledger import DeliveryLedger
ledger = DeliveryLedger(Path({str(path)!r}))
ledger.record("k", "chat", "never started")
a = ledger.record("k", "chat", "mid-flight", reply_to="m9", thread_id="t")
ledger.mark_attempting(a.obligation_id)
f = ledger.record("k", "chat", "rejected")
ledger.mark_failed(f.obligation_id, "boom")
d = ledger.record("k", "chat", "done")
ledger.mark_delivered(d.obligation_id)
import os; os._exit(0)  # simulate a crash: no cleanup
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
    ledger = DeliveryLedger(path)
    recovered = ledger.sweep_recoverable()
    by_text = {o.content: o for o in recovered}
    assert set(by_text) == {"never started", "mid-flight", "rejected"}
    assert by_text["never started"].recovered_content == "never started"
    assert by_text["mid-flight"].recovered_content.startswith(RECOVERED_MARKER)
    assert by_text["mid-flight"].reply_to == "m9" and by_text["mid-flight"].thread_id == "t"
    assert by_text["rejected"].recovered_content.startswith(RECOVERED_MARKER)
    # Now owned by this process: a second sweep finds nothing.
    assert ledger.sweep_recoverable() == []


async def test_runner_records_deliveries_and_resends_on_startup(tmp_path):
    adapter = FakeAdapter()
    gateway = GatewayRunner(config(), options(tmp_path), adapter, provider_factory=ReplyProvider)
    await gateway.start()
    try:
        await adapter.deliver("hello")
        await adapter.wait_idle()
        rows = gateway.ledger.rows()
        assert [r.state for r in rows] == ["delivered"] and rows[0].content == "reply: hello"
        # Leave an undelivered obligation behind as if the process died mid-send.
        stale = gateway.ledger.record("fake:dm:chat", "chat", "lost reply", reply_to="m0")
        gateway.ledger.mark_attempting(stale.obligation_id)
    finally:
        await gateway.stop()
    # A "new process": rewrite the owner stamp so the sweep treats it as dead.
    import sqlite3

    with sqlite3.connect(tmp_path / "state" / "gateway" / "deliveries.sqlite3") as c:
        c.execute("UPDATE delivery_obligations SET owner_pid=999999, owner_identity='gone'")
    adapter2 = FakeAdapter()
    gateway2 = GatewayRunner(config(), options(tmp_path), adapter2, provider_factory=ReplyProvider)
    await gateway2.start()
    try:
        assert gateway2.recovered_deliveries == 1
        assert adapter2.sent == [("chat", RECOVERED_MARKER + "lost reply", "m0")]
        assert all(r.state == "delivered" for r in gateway2.ledger.rows())
    finally:
        await gateway2.stop()


# --- heartbeats ----------------------------------------------------------------------


async def test_heartbeat_store_and_scheduler_fire_due_jobs(tmp_path):
    now = [1000.0]
    store = HeartbeatStore(tmp_path / "hb.json", clock=lambda: now[0])
    source = SessionSource("fake", "chat", "dm", user_id="alice")
    repeating = store.add("k", source, "check ci", interval_seconds=60, first_run_at=1050)
    once = store.add("k", source, "remind me", interval_seconds=None, first_run_at=1010)
    with pytest.raises(ValueError):
        store.add("k", source, "too fast", interval_seconds=5)
    fired: list[str] = []

    async def wake(job):
        fired.append(job.job_id)
        if job.job_id == once.job_id:
            raise RuntimeError("wake failed")

    scheduler = HeartbeatScheduler(store, wake)
    assert await scheduler.tick() == 0
    now[0] = 1011
    assert await scheduler.tick() == 1 and fired == [once.job_id]
    assert store.get(once.job_id) is None  # one-shot jobs are removed even on failure
    now[0] = 1051
    assert await scheduler.tick() == 1
    job = store.get(repeating.job_id)
    assert job is not None and job.runs == 1 and job.next_run_at == 1110 and job.last_error == ""
    reloaded = HeartbeatStore(tmp_path / "hb.json")
    assert [j.job_id for j in reloaded.for_session("k")] == [repeating.job_id]
    assert reloaded.get(repeating.job_id).source.user_id == "alice"


async def test_heartbeat_commands_and_wake_run_on_the_chat_session(tmp_path):
    adapter = FakeAdapter()
    gateway = GatewayRunner(config(), options(tmp_path), adapter, provider_factory=ReplyProvider)
    await gateway.start()
    try:
        await adapter.deliver("/heartbeat add 0.5 nope")
        await adapter.wait_idle()
        assert "至少 1 分钟" in adapter.sent[-1][1]
        await adapter.deliver("/heartbeat add 1 看看测试结果")
        await adapter.wait_idle()
        assert adapter.sent[-1][1].startswith("已创建心跳")
        (job,) = gateway.heartbeats.for_session("fake:dm:chat")
        await adapter.deliver("/heartbeat list")
        await adapter.wait_idle()
        assert job.job_id in adapter.sent[-1][1]
        # Make it due and tick the scheduler by hand.
        job.next_run_at = time.time() - 1
        assert await gateway._heartbeat_scheduler.tick() == 1
        await adapter.wait_idle()
        chat, text, reply_to = adapter.sent[-1]
        assert chat == "chat" and reply_to is None
        assert text == f"reply: [定时任务 {job.job_id}] 看看测试结果"
        assert job.runs == 1
        await adapter.deliver(f"/heartbeat remove {job.job_id}")
        await adapter.wait_idle()
        assert not gateway.heartbeats.for_session("fake:dm:chat")
    finally:
        await gateway.stop()


# --- stall detection -----------------------------------------------------------------


def test_stall_monitor_reports_once_per_quiet_period():
    now = [0.0]
    monitor = StallMonitor(10, clock=lambda: now[0])
    monitor.begin("k")
    now[0] = 5
    monitor.progress("k", "调用 bash")
    now[0] = 14
    assert monitor.stalled() == []
    now[0] = 16
    (report,) = monitor.stalled()
    assert report.session_key == "k" and report.last_label == "调用 bash"
    assert "/stop" in format_stall_notice(report)
    assert monitor.stalled() == []  # already notified
    now[0] = 17
    monitor.progress("k", "bash 完成")
    now[0] = 30
    assert len(monitor.stalled()) == 1  # a new quiet period after progress
    assert monitor.end("k") is not None and monitor.stalled() == []


async def test_runner_notifies_a_stalled_turn_in_the_chat(tmp_path):
    adapter = FakeAdapter()
    blocking = BlockingProvider()
    gateway = GatewayRunner(
        config(stall_timeout_seconds=10),
        options(tmp_path),
        adapter,
        provider_factory=lambda: blocking,
    )
    await gateway.start()
    try:
        await adapter.deliver("slow")
        await asyncio.wait_for(blocking.started.wait(), 10)
        assert await gateway.check_stalls() == 0
        gateway.stalls.activity("fake:dm:chat").last_progress_at -= 30
        assert await gateway.check_stalls() == 1
        assert "没有新进展" in adapter.sent[-1][1]
        assert await gateway.check_stalls() == 0
        blocking.release.set()
        await adapter.wait_idle()
        assert gateway.stalls.activity("fake:dm:chat") is None
    finally:
        await gateway.stop()
