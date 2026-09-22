"""Project indexes are the source of truth; the global catalog is a rebuildable cache."""

from __future__ import annotations

import json
from pathlib import Path

from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.session_manager import SessionManager


def _paths(tmp_path: Path) -> RunAgentPaths:
    return RunAgentPaths(home=tmp_path / "state", agents_home=tmp_path / "agents")


async def test_rebuild_catalog_restores_records_from_project_index(tmp_path: Path) -> None:
    manager = SessionManager(_paths(tmp_path))
    try:
        created = await manager.create_session(cwd=tmp_path, model="test-model")
        assert await manager.get_session(created.id) is not None

        catalog = manager._catalog_path()
        assert catalog.is_file()
        catalog.unlink()
        assert [record.id for record in await manager.list_sessions(None)] == []
        assert await manager.get_session(created.id) is None

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
