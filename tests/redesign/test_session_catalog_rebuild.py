"""Project indexes are the source of truth; the global catalog is a rebuildable cache.

The catalog is a derived cache, so a missing, corrupt, or outdated one must repair itself
from a *known* project index at the point the manager is first asked about that project -
never by scanning the disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.session_manager import SessionManager


def _paths(tmp_path: Path) -> RunAgentPaths:
    return RunAgentPaths(home=tmp_path / "state", agents_home=tmp_path / "agents")


def _catalog_rows(manager: SessionManager) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in manager._catalog_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_rebuild_catalog_restores_records_from_project_index(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        assert await manager.get_session(created.id) is not None

        catalog = manager._catalog_path()
        assert catalog.is_file()
        catalog.unlink()

        # A catalog read repairs the cache from the project index of the known directory.
        rebuilt = manager.rebuild_catalog([tmp_path])
        assert [record.id for record in rebuilt] == [created.id]

        restored = await manager.get_session(created.id)
        assert restored is not None
        assert restored.id == created.id
        assert restored.cwd == created.cwd
        assert restored.model == created.model
        assert restored.path == created.path
        assert restored.created_at == created.created_at
        assert restored.updated_at == created.updated_at
        assert [record.id for record in await manager.list_sessions(None)] == [created.id]
    finally:
        await manager.aclose()


async def test_missing_catalog_is_rebuilt_on_the_first_read(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        manager._catalog_path().unlink()

        # No explicit rebuild call: reading the catalog is the startup repair entry point.
        assert [record.id for record in await manager.list_sessions(None)] == [created.id]
        assert manager._catalog_path().is_file()
        assert await manager.get_session(created.id) is not None
    finally:
        await manager.aclose()


async def test_project_scoped_startup_read_rebuilds_a_missing_catalog(tmp_path: Path) -> None:
    """A fresh process with a deleted catalog recovers through list_sessions(cwd)."""
    first = SessionManager(_paths(tmp_path))
    try:
        created = await first.create_session(cwd=tmp_path, model="test-model")
    finally:
        await first.aclose()
    first._catalog_path().unlink()

    second = SessionManager(_paths(tmp_path))
    try:
        assert [record.id for record in await second.list_sessions(tmp_path)] == [created.id]
        assert second._catalog_path().is_file()
        assert await second.get_session(created.id) is not None
    finally:
        await second.aclose()


async def test_get_session_with_cwd_resolves_without_a_catalog(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        manager._catalog_path().unlink()

        found = await manager.get_session(created.id, cwd=tmp_path)

        assert found is not None
        assert found.path == created.path
        assert [row["id"] for row in _catalog_rows(manager)] == [created.id]
    finally:
        await manager.aclose()


async def test_outdated_catalog_is_corrected_by_the_project_index(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="old-model")
        catalog = manager._catalog_path()
        project_index = manager.project_index_path(tmp_path)

        # Another writer updated only the project index, as a crash between the two writes
        # would leave it; the catalog must lose that race.
        fresher = replace(
            created, model="fresh-model", title="fresh", updated_at=created.updated_at + 10
        )
        with project_index.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(fresher.to_json()) + "\n")
        os.utime(catalog, (0, 0))
        assert [row["model"] for row in _catalog_rows(manager)] == ["old-model"]

        found = await manager.get_session(created.id)
        assert [record.model for record in manager._read_index(catalog)] == ["fresh-model"]
        assert found is not None
        assert found.model == "fresh-model"
        assert [row["model"] for row in _catalog_rows(manager)] == ["old-model", "fresh-model"]
    finally:
        await manager.aclose()


async def test_corrupt_catalog_is_rewritten_from_the_project_index(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        catalog = manager._catalog_path()
        catalog.write_text('{not json\n{"id": "torn"\n', encoding="utf-8")

        records = await manager.list_sessions(None)

        assert [record.id for record in records] == [created.id]
        rows = _catalog_rows(manager)
        assert [row["id"] for row in rows] == [created.id]
        assert await manager.get_session(created.id) is not None
    finally:
        await manager.aclose()


async def test_rebuild_catalog_is_idempotent(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        await manager.create_session(cwd=tmp_path, model="test-model")
        await manager.create_session(cwd=tmp_path, model="other-model")
        manager._catalog_path().unlink()

        first = manager.rebuild_catalog([tmp_path])
        second = manager.rebuild_catalog([tmp_path])
        assert {record.id for record in first} == {record.id for record in second}
        assert len(second) == len(first) == 2
        assert len(await manager.list_sessions(None)) == 2
    finally:
        await manager.aclose()


async def test_rebuild_catalog_ignores_unknown_cwd(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        manager._catalog_path().unlink()
        foreign = tmp_path / "elsewhere"
        foreign.mkdir()
        assert manager.rebuild_catalog([foreign]) == []
        assert [record.id for record in manager.rebuild_catalog([foreign, tmp_path])] == [
            created.id
        ]
    finally:
        await manager.aclose()


async def test_read_all_records_discovers_nested_index_jsonl(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        nested = manager.paths.sessions_dir / "nested" / "deeper" / "index.jsonl"
        nested.parent.mkdir(parents=True)
        payload = {
            "id": "nested-session",
            "path": "nested-session.jsonl",
            "cwd": str(tmp_path),
            "model": "nested-model",
            "title": None,
            "created_at": 1.0,
            "updated_at": 2.0,
        }
        nested.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        listed = await manager.list_sessions(None)
        assert [record.id for record in listed] == ["nested-session"]
        assert listed[0].model == "nested-model"
    finally:
        await manager.aclose()
