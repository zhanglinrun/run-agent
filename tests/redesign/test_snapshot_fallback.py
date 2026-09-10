"""P2-6: a snapshot is derived acceleration, so losing it must not lose history.

Plan 7.6 requires that a missing snapshot or an incompatible builder falls back
to rebuilding from the raw event history, and that the snapshot never becomes a
second source of truth.

Deleting a referenced snapshot is not reachable on purpose: executions.snapshot_id
is a foreign key, so a snapshot that backed an execution cannot be orphaned. The
reachable failure modes are therefore an incompatible builder and a snapshot that
can no longer be decoded, and both are covered here.
"""

from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import options
from tests.redesign.test_context_snapshots import RecordingProvider

from run_agent_coding.application import CodingApplication
from run_agent_coding.storage.snapshots import read_snapshot
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import UserMessage
from run_agent_core.session.contracts import SessionConflict


def user_texts(app) -> list[str]:
    return [message.text for message in app.session.messages if isinstance(message, UserMessage)]


def marker_reasons(app) -> list[str]:
    return [
        entry.data["reason"]
        for entry in app.session._state.custom_entries
        if entry.namespace == "run.resources"
    ]


@pytest.fixture
def skill_root(tmp_path):
    root = options(tmp_path).paths.home / "skills" / "example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\ndescription: v1\n---\nbody v1\n", encoding="utf-8")
    return root


async def prepare(tmp_path):
    opts = options(tmp_path)
    app = await CodingApplication.open(opts, provider=RecordingProvider())
    await app.start()
    _ = [event async for event in app.prompt("first task")]
    _ = [event async for event in app.prompt("second task")]
    record = (app.session.session_id, app.session.current_snapshot_id, user_texts(app))
    record += (app.session.skills[0].package_digest, marker_reasons(app))
    await app.aclose()
    return (opts, *record)


async def test_incompatible_builder_version_still_restores_from_events(tmp_path, skill_root):
    opts, session_id, _snapshot_id, texts, digest, reasons = await prepare(tmp_path)
    assert reasons == ["startup"]

    async with await SqliteDatabase.open(opts.paths.database_path) as database:
        changed = await database.run(
            lambda connection: (
                connection.execute(
                    "UPDATE context_snapshots SET builder_version='coding-input-v999'"
                ).rowcount
            )
        )
    assert changed >= 1

    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=RecordingProvider()
    ) as resumed:
        await resumed.start()
        assert user_texts(resumed) == texts
        assert resumed.session.skills[0].package_digest == digest
        # Resume records its own activation marker; history itself is unchanged.
        assert marker_reasons(resumed) == ["startup", "resume"]


async def test_an_undecodable_snapshot_does_not_block_resume(tmp_path, skill_root):
    opts, session_id, snapshot_id, texts, digest, _reasons = await prepare(tmp_path)

    async with await SqliteDatabase.open(opts.paths.database_path) as database:
        # Valid JSON, so the schema's json_valid check still passes, but the body
        # no longer matches the digest it was stored under.
        await database.run(
            lambda connection: connection.execute(
                "UPDATE snapshot_blocks SET body_json='\"tampered\"'"
            )
        )
        # The corruption is real: the snapshot can no longer be read back.
        with pytest.raises((SessionConflict, KeyError)):
            await database.run(lambda connection: read_snapshot(connection, snapshot_id))

    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=RecordingProvider()
    ) as resumed:
        await resumed.start()
        assert user_texts(resumed) == texts
        assert resumed.session.skills[0].package_digest == digest
        assert marker_reasons(resumed) == ["startup", "resume"]


async def test_tampered_context_blocks_are_refused_on_the_next_write(tmp_path, skill_root):
    """Blocks are a deduplicated store, so damaging one must be detected."""
    opts, session_id, _snapshot_id, _texts, _digest, _reasons = await prepare(tmp_path)

    async with await SqliteDatabase.open(opts.paths.database_path) as database:
        await database.run(
            lambda connection: connection.execute(
                "UPDATE snapshot_blocks SET body_json='\"tampered\"'"
            )
        )

    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=RecordingProvider()
    ) as resumed:
        await resumed.start()
        with pytest.raises(SessionConflict, match="content hash mismatch"):
            _ = [event async for event in resumed.prompt("third task")]
