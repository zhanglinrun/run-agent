"""S03: rebuilding from full history must agree with a recorded snapshot.

The plan's 7.6 boundary is that a snapshot is derived acceleration, not a second
source of truth. Rebuilding the logical messages and the resource reference from
the raw event history at a snapshot's leaf must therefore match what the
snapshot recorded, without re-running a model to regenerate a summary.
"""

import pytest
from tests.redesign.test_coding_application import options
from tests.redesign.test_context_snapshots import RecordingProvider

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

        snapshot = await app.session.storage.repository.get_snapshot(snapshot_id)
        assert snapshot["payload"]["resource_snapshot_id"] == resource_at_snapshot

        entries = (await app.session.storage.read_entries()).entries
        assert len(entries) > 4, "the session should have grown past the snapshot"
        rebuilt = SessionState.from_entries(list(entries), leaf_id=head_at_snapshot)

        assert [message.text for message in rebuilt.messages] == messages_at_snapshot
        assert resource_events(rebuilt)[-1].id == resource_at_snapshot
