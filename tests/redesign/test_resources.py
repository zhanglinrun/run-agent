"""Cross-session scopes, failed promotion and stale extension fencing."""

import asyncio

import pytest

from run_agent_coding.host.contracts import ArtifactRef, ExtensionToken, HeadChange, StateChange
from run_agent_coding.storage.artifacts import ArtifactCorrupt, ArtifactStore
from run_agent_coding.storage.resources import NamespaceResources
from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.storage.state import (
    ExtensionRetired,
    NamespaceState,
    activate_extension,
    retire_extension,
)
from run_agent_core.session.contracts import SessionConflict


@pytest.fixture
async def resources(tmp_path):
    async with await SqliteDatabase.open(tmp_path / "state.sqlite3") as database:
        sessions = SqliteSessionRepository(database)
        await sessions.create_session(
            cwd=tmp_path, principal_id="alice", model="test", session_id="s"
        )
        await sessions.claim("s", owner_id="host", run_id="r")
        token = ExtensionToken("s", "experience", "host", 1)
        await activate_extension(database, token)
        artifacts = ArtifactStore(tmp_path / "artifacts")
        state = NamespaceState(database, token, "alice/project-a")
        versions = NamespaceResources(database, token, "alice/project-a", artifacts)
        yield database, token, state, versions, artifacts


async def test_cas_rejects_lost_updates_and_atomic_batch_rolls_back(resources):
    _, _, state, _, _ = resources
    await state.compare_and_set(StateChange("memory", 0, "first"))
    outcomes = await asyncio.gather(
        state.compare_and_set(StateChange("memory", 1, "second")),
        state.compare_and_set(StateChange("memory", 1, "third")),
        return_exceptions=True,
    )
    assert sum(isinstance(item, SessionConflict) for item in outcomes) == 1
    assert (await state.get("memory")).version == 2
    with pytest.raises(SessionConflict):
        await state.apply_batch([StateChange("new", 0, 1), StateChange("memory", 1, "stale")])
    assert await state.get("new") is None


async def test_source_scope_and_principal_isolation(resources):
    database, token, state, _, _ = resources
    await state.compare_and_set(StateChange("fact", 0, "project-a fact"))
    for scope in ["alice/project-b", "bob/project-a"]:
        assert await NamespaceState(database, token, scope).get("fact") is None
    second = ExtensionToken("s", "observability", "host", 1)
    await activate_extension(database, second)
    assert await NamespaceState(database, second, "alice/project-a").get("fact") is None
    assert [item.key for item in await state.list(prefix="fa")] == ["fact"]
    assert await state.list(prefix="%") == []


async def test_reload_retired_worker_cannot_write_or_publish(resources):
    database, token, state, versions, artifacts = resources
    candidate = await versions.put_immutable("skill", "Check output.")
    replacement = ExtensionToken("s", "experience", "host", 2)
    await activate_extension(database, replacement)
    with pytest.raises(ExtensionRetired):
        await state.compare_and_set(StateChange("late", 0, "old worker"))
    with pytest.raises(ExtensionRetired):
        await versions.advance_head(HeadChange("skill", None, candidate.version, "publish", {}))
    await retire_extension(database, token)
    new_versions = NamespaceResources(database, replacement, "alice/project-a", artifacts)
    await new_versions.advance_head(HeadChange("skill", None, candidate.version, "publish", {}))
    assert (await new_versions.snapshot())["skill"] == candidate.version
    await retire_extension(database, replacement)
    with pytest.raises(ExtensionRetired):
        await activate_extension(database, replacement)


async def test_new_host_invalidates_old_namespace_even_before_rebinding(resources):
    database, token, state, _, _ = resources
    await SqliteSessionRepository(database).claim(
        "s", owner_id="new-host", run_id="new", takeover=True
    )
    with pytest.raises(ExtensionRetired):
        await state.compare_and_set(StateChange("late", 0, "old host"))
    with pytest.raises(ExtensionRetired):
        await activate_extension(database, ExtensionToken("s", token.source_id, "host", 100))


async def test_resource_snapshot_resolves_frozen_content_after_publish_and_rollback(resources):
    database, _, _, versions, _ = resources
    first = await versions.put_immutable("skill", "Version one")
    await versions.advance_head(HeadChange("skill", None, first.version, "manual", {}))
    frozen = await versions.snapshot()
    second = await versions.put_immutable("skill", "Version two", parent_version=first.version)
    await versions.advance_head(
        HeadChange("skill", first.version, second.version, "evaluated", {"report": "r"})
    )
    assert (await versions.resolve("skill", frozen["skill"])).content == "Version one"
    await versions.advance_head(
        HeadChange("skill", second.version, first.version, "rollback", {"reason": "regression"})
    )
    assert await versions.snapshot() == frozen
    assert (
        await database.run(
            lambda connection: connection.execute(
                "SELECT count(*) FROM resource_publications"
            ).fetchone()[0]
        )
        == 3
    )


async def test_candidate_status_and_live_pointer_commit_atomically(resources):
    _, _, state, versions, _ = resources
    candidate = await versions.put_immutable("skill", "Candidate")
    await state.compare_and_set(StateChange("candidate", 0, "evaluated"))
    with pytest.raises(SessionConflict):
        await state.apply_batch(
            [StateChange("candidate", 1, "promoted")],
            [HeadChange("skill", "wrong-base", candidate.version, "publish", {})],
        )
    assert (await state.get("candidate")).value == "evaluated"
    assert await versions.snapshot() == {}
    await state.apply_batch(
        [StateChange("candidate", 1, "promoted")],
        [HeadChange("skill", None, candidate.version, "publish", {"evidence": "verified"})],
    )
    assert (await state.get("candidate")).value == "promoted"
    assert (await versions.snapshot())["skill"] == candidate.version


async def test_file_written_before_failed_version_commit_is_unreferenced(resources):
    database, _, _, versions, artifacts = resources
    ref = await artifacts.put(b"immutable script")
    with pytest.raises(SessionConflict):
        await versions.put_immutable("skill", "broken", parent_version="missing", artifacts=[ref])
    assert await artifacts.read(ref) == b"immutable script"
    assert await versions.snapshot() == {}
    assert (
        await database.run(
            lambda connection: connection.execute("SELECT count(*) FROM artifact_refs").fetchone()[
                0
            ]
        )
        == 0
    )


async def test_missing_or_corrupt_artifacts_never_gain_references(resources):
    database, _, _, versions, artifacts = resources
    with pytest.raises(ArtifactCorrupt):
        await versions.put_immutable("missing", "content", artifacts=[ArtifactRef("a" * 64, 10)])
    ref = await artifacts.put(b"source")
    artifacts.path(ref.digest).write_bytes(b"edited")
    with pytest.raises(ArtifactCorrupt):
        await versions.put_immutable("changed", "content", artifacts=[ref])
    assert (
        await database.run(
            lambda connection: connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        )
        == 0
    )


async def test_same_scope_persists_across_sessions(resources, tmp_path):
    database, _, state, _, _ = resources
    await state.compare_and_set(StateChange("preference", 0, "Chinese"))
    sessions = SqliteSessionRepository(database)
    await sessions.create_session(cwd=tmp_path, principal_id="alice", model="test", session_id="s2")
    await sessions.claim("s2", owner_id="host2", run_id="r2")
    token = ExtensionToken("s2", "experience", "host2", 1)
    await activate_extension(database, token)
    assert (
        await NamespaceState(database, token, "alice/project-a").get("preference")
    ).value == "Chinese"
