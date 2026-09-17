"""S03 snapshot/full-history equivalence and S06 skill commit failure.

S03 - the plan's 7.6 boundary: a snapshot is derived acceleration, not a second
      source of truth, so rebuilding the logical messages and the resource
      reference from raw history at a snapshot's leaf must match the snapshot.
S06 - a skill package may be written but its version commit can still fail. When
      it does, the effective pointer must not move and no half-published state
      may survive a reopen.
"""

from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import RecordingProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_core.session.memory import SessionState


def resource_events(state):
    return [entry for entry in state.custom_entries if entry.namespace == "run.resources"]


@pytest.fixture
def skill_root(tmp_path):
    root = options(tmp_path).paths.home / "skills" / "example"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\ndescription: stable v1\n---\nUse refs/note.md\n", encoding="utf-8"
    )
    (root / "refs").mkdir()
    (root / "refs/note.md").write_text("reference v1", encoding="utf-8")
    return root


async def test_full_history_rebuild_matches_the_recorded_snapshot(tmp_path, skill_root):
    async with await CodingApplication.open(options(tmp_path), provider=RecordingProvider()) as app:
        events = [event async for event in app.prompt("first task")]
        snapshot_id = events[-1].snapshot_id
        head_at_snapshot = events[-1].head_id
        messages_at_snapshot = [message.text for message in app.session.messages]
        resource_at_snapshot = resource_events(app.session._state)[-1].id

        # History keeps growing after the snapshot was taken.
        _ = [event async for event in app.prompt("second task")]
        await app.command("/name a longer session")

        snapshot = await app.session.storage.get_snapshot(snapshot_id)
        assert snapshot["payload"]["resource_snapshot_id"] == resource_at_snapshot

        entries = (await app.session.storage.read_entries()).entries
        assert len(entries) > 4, "the session should have grown past the snapshot"
        rebuilt = SessionState.from_entries(list(entries), leaf_id=head_at_snapshot)

        assert [message.text for message in rebuilt.messages] == messages_at_snapshot
        assert resource_events(rebuilt)[-1].id == resource_at_snapshot


async def test_host_services_exposes_a_default_off_fault_hook(tmp_path, skill_root):
    async with await CodingApplication.open(options(tmp_path), provider=RecordingProvider()) as app:
        await app.start()
        assert app.session.host_services.fault is None


async def test_version_commit_failure_keeps_the_effective_pointer(tmp_path, skill_root):
    opts = options(tmp_path)
    app = await CodingApplication.open(opts, provider=RecordingProvider())
    await app.start()
    _ = [event async for event in app.prompt("first task")]
    head_before = (await app.session.storage.get_head()).entry_id
    session_id = app.session.session_id
    original = app.session.skills[0].package_digest
    assert [entry.data["reason"] for entry in resource_events(app.session._state)] == ["startup"]

    # /reload freezes a new package and commits it as the effective version.
    (skill_root / "SKILL.md").write_text("next version", encoding="utf-8")

    def fail(point: str) -> None:
        if point == "activation_commit":
            raise OSError("version commit failure")

    app.session.host_services.fault = fail
    try:
        with pytest.raises(OSError, match="version commit failure"):
            await app.command("/reload")
    finally:
        app.session.host_services.fault = None

    # The persisted effective version must not move on a failed commit.
    assert (await app.session.storage.get_head()).entry_id == head_before
    assert [entry.data["reason"] for entry in resource_events(app.session._state)] == ["startup"]
    await app.aclose()

    # Nothing half-published survives: a reopen restores the old version only.
    async with await CodingApplication.open(
        replace(opts, resume=session_id), provider=RecordingProvider()
    ) as reopened:
        await reopened.start()
        assert reopened.session.skills[0].package_digest == original
