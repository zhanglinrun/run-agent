"""Session save/load under .run/sessions."""

from __future__ import annotations

import json
from datetime import datetime
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


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    """追加折叠审计行，并写入最新快照到 .run/sessions/。"""
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
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
    """找最近改过的那个会话 id（供 --resume 使用）。"""
    items = list_sessions(limit=1)
    return items[0]["id"] if items else None


def list_sessions(*, limit: int = 20) -> list[dict[str, Any]]:
    """按修改时间倒序列出会话摘要，供 /sessions 与交互式 /resume 使用。"""
    d = get_project_session_dir()
    files = sorted(
        (p for p in d.glob("*.json") if ".folded-memory" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for path in files[: max(0, limit)]:
        data = load_session(path.stem) or {}
        messages = data.get("messages") if isinstance(data.get("messages"), list) else []
        preview = _preview_user_message(messages)
        mtime = path.stat().st_mtime
        out.append(
            {
                "id": path.stem,
                "model": data.get("model") or "",
                "message_count": len(messages),
                "preview": preview,
                "updated_at": mtime,
                "updated_at_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return out


def _preview_user_message(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            text = " ".join(content.strip().split())
            return text if len(text) <= 60 else text[:57] + "..."
    return "(no user message)"
