"""Actual input evidence, crash boundaries and scoped background snapshot access."""

import asyncio
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.host.contracts import TaskSpec
from run_agent_coding.storage.tasks import TaskRejected
from run_agent_core.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage
from run_agent_core.session.contracts import RunOutcome, SessionConflict


class RecordingProvider(ReplyProvider):
    def __init__(self):
        self.requests = []

    async def stream_response(self, *, model, system, messages, tools, **kwargs):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.parameters),
                        "execution_mode": tool.execution_mode,
                    }
                    for tool in tools
                ],
            }
        )
        async for event in super().stream_response(messages=messages, **kwargs):
            yield event


async def all_snapshots(app):
    repository = app.session.storage.repository
    ids = await repository.database.run(
        lambda connection: [
            row[0]
            for row in connection.execute(
                "SELECT snapshot_id FROM context_snapshots ORDER BY rowid"
            )
        ]
    )
    return [await repository.get_snapshot(identity) for identity in ids]


async def test_exact_requests_after_transform_and_repair_including_auxiliary_calls(tmp_path):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:

        async def transform(messages, signal):
            return [
                *messages,
                AssistantMessage(content=[], stop_reason="error", error_message="old failure"),
                AssistantMessage(content=[ToolCall(id="missing", name="read", arguments={})]),
                ToolResultMessage(
                    tool_call_id="orphan", tool_name="read", content=[TextContent(text="unmatched")]
                ),
            ]

        app.session._harness.config.transform_context = transform
        events = [event async for event in app.prompt("snapshot this")]
        receipt = events[-1]
        assert isinstance(receipt, AgentSettledEvent) and receipt.status == "succeeded"
        task_snapshot = await app.session.storage.repository.get_snapshot(receipt.snapshot_id)
        assert task_snapshot["payload"]["purpose"] == "agent"
        assert task_snapshot["run_id"] == receipt.run_id
        repaired = task_snapshot["payload"]["messages"]
        assert any(message.get("toolCallId") == "missing" for message in repaired)
        assert not any(message.get("toolCallId") == "orphan" for message in repaired)
        first_head = receipt.head_id
        await app.command("/compact")
        _ = [event async for event in app.prompt("branch this later turn")]
        await app.session.branch_to_entry(first_head, summarize=True)
        snapshots = await all_snapshots(app)
        assert {row["payload"]["purpose"] for row in snapshots} == {
            "agent",
            "session_name",
            "compaction",
            "branch_summary",
        }
        assert len(snapshots) == len(provider.requests)
        for row, request in zip(snapshots, provider.requests, strict=True):
            assert {key: row["payload"][key] for key in request} == request
        durable = await app.session.storage.repository.database.run(
            lambda connection: connection.execute(
                "SELECT snapshot_id FROM executions WHERE run_id=?", (receipt.run_id,)
            ).fetchone()[0]
        )
        assert durable == receipt.snapshot_id


async def test_snapshot_failure_prevents_uncaptured_provider_request(tmp_path, monkeypatch):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:

        async def fail(*args, **kwargs):
            raise OSError("snapshot disk failure")

        monkeypatch.setattr(app.session.storage, "record_context", fail)
        events = []
        with pytest.raises(OSError, match="snapshot disk failure"):
            async for event in app.prompt("must not call model"):
                events.append(event)
        assert provider.requests == []
        assert not any(
            isinstance(event, AgentSettledEvent) and event.status == "succeeded" for event in events
        )
        statuses = await app.session.storage.repository.database.run(
            lambda connection: [
                row[0] for row in connection.execute("SELECT status FROM executions")
            ]
        )
        assert statuses == ["failed"]


async def test_cancel_after_snapshot_commit_has_evidence_but_no_provider_call(
    tmp_path, monkeypatch
):
    provider = RecordingProvider()
    async with await CodingApplication.open(options(tmp_path), provider=provider) as app:
        await app.command("/name no automatic name")
        original = app.session.storage.record_context
        committed, release = asyncio.Event(), asyncio.Event()

        async def delay(*args, **kwargs):
            result = await original(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        monkeypatch.setattr(app.session.storage, "record_context", delay)

        async def consume():
            return [event async for event in app.prompt("cancel before provider")]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.requests == []
        snapshots = await all_snapshots(app)
        assert len(snapshots) == 1
        row = await app.session.storage.repository.database.run(
            lambda connection: tuple(
                connection.execute("SELECT status,snapshot_id FROM executions").fetchone()
            )
        )
        assert row == ("cancelled", snapshots[0]["snapshot_id"])


async def test_completion_rejects_snapshot_from_previous_run_or_other_branch(tmp_path):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        events = [event async for event in app.prompt("first")]
        previous = events[-1].snapshot_id
        storage = app.session.storage
        head = (await storage.get_head()).entry_id
        token = await storage.begin_run("next")
        with pytest.raises(SessionConflict, match="does not belong"):
            await storage.complete_run(
                RunOutcome(token, storage.branch_id, "succeeded", head, snapshot_id=previous)
            )
        snapshot = await storage.record_context(
            {}, token=token, expected_head=head, builder_version="test"
        )
        await storage.fork(head, token=token)
        with pytest.raises(SessionConflict, match="does not match"):
            await storage.complete_run(
                RunOutcome(token, storage.branch_id, "succeeded", head, snapshot_id=snapshot)
            )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_shared_blocks_are_deduplicated_and_verified(tmp_path, damage):
    async with await CodingApplication.open(options(tmp_path), provider=ReplyProvider()) as app:
        storage = app.session.storage
        head = (await storage.get_head()).entry_id
        payload = {"messages": [{"text": "old"}], "system": "fixed"}
        first = await storage.record_context(
            payload, token=storage.token, expected_head=head, builder_version="test"
        )
        payload["messages"].append({"text": "new"})
        second = await storage.record_context(
            payload, token=storage.token, expected_head=head, builder_version="test"
        )
        db = storage.repository.database
        assert (
            await db.run(
                lambda connection: connection.execute(
                    "SELECT COUNT(*) FROM snapshot_blocks"
                ).fetchone()[0]
            )
            == 3
        )
        original = await storage.repository.get_snapshot(first)
        assert original["payload"]["messages"] == [{"text": "old"}]
        assert (await storage.repository.get_snapshot(second))["payload"] == payload
        query = (
            "DELETE FROM snapshot_blocks WHERE body_json='\"fixed\"'"
            if damage == "missing"
            else "UPDATE snapshot_blocks SET body_json='{}' WHERE body_json='\"fixed\"'"
        )
        await db.run(lambda connection: connection.execute(query), write=True)
        with pytest.raises(SessionConflict, match="missing or corrupt"):
            await storage.repository.get_snapshot(first)


async def test_tasks_read_fixed_snapshot_and_reject_foreign_or_missing_input(tmp_path):
    extension = tmp_path / "snapshot_reader.py"
    extension.write_text(
        """
def setup(api):
    async def inspect(payload, context):
        snapshot = await context.services.snapshots.read(context.snapshot_id)
        return snapshot.payload["model"]
    api.register_task_handler("inspect", inspect)
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as first:
        events = [event async for event in first.prompt("fixed evidence")]
        snapshot_id = events[-1].snapshot_id
        service = context(first).services
        snapshot = await service.snapshots.read(snapshot_id)
        snapshot.payload["model"] = "mutated copy"
        assert (await service.snapshots.read(snapshot_id)).payload["model"] == "test"
        task_id = await service.tasks.submit(TaskSpec("inspect", {}, snapshot_id))
        async with asyncio.timeout(5):
            while (status := await service.tasks.status(task_id)).status in {"queued", "running"}:
                await asyncio.sleep(0.01)
        assert status.status == "succeeded" and status.result == "test"
        async with await CodingApplication.open(opts, provider=ReplyProvider()) as second:
            await second.start()
            other = context(second).services
            for identity in (snapshot_id, "missing"):
                with pytest.raises(TaskRejected, match="another session"):
                    await other.tasks.submit(TaskSpec("inspect", {}, identity))
                with pytest.raises(KeyError, match="another session"):
                    await other.snapshots.read(identity)
