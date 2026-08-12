"""Session save/load under .run/sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def get_project_session_dir() -> Path:
    """会话文件目录：项目下的 .run/sessions/（没有就创建）。"""
    d = Path.cwd() / ".run" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    """把一次对话状态存成 JSON 文件，例如 demo.json。"""
    path = get_project_session_dir() / f"{session_id}.json"
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    """按 id 读回会话；没有文件或坏了就返回 None。"""
    path = get_project_session_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_latest_session_id() -> str | None:
    """找最近改过的那个会话 id（以后 --resume 会用到）。"""
    d = get_project_session_dir()
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None
