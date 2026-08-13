#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

SESSION_DIR = Path.home() / ".run-agent" / "sessions"


def _ensure_dir() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".run" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    (SESSION_DIR / f"{session_id}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_sessions(*, limit: int = 20) -> list[dict[str, Any]]:
    _ensure_dir()
    files = sorted(
        (p for p in SESSION_DIR.glob("*.json") if ".folded-memory" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for path in files[: max(0, limit)]:
        data = load_session(path.stem) or {}
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        messages = data.get("openaiMessages") or data.get("messages") or []
        if not messages and isinstance(data.get("anthropicMessages"), list):
            messages = data["anthropicMessages"]
        mtime = path.stat().st_mtime
        out.append(
            {
                "id": meta.get("id") or path.stem,
                "model": meta.get("model") or data.get("model") or "",
                "message_count": meta.get("messageCount")
                or (len(messages) if isinstance(messages, list) else 0),
                "preview": _preview_user_message(messages if isinstance(messages, list) else []),
                "startTime": meta.get("startTime") or "",
                "updated_at": mtime,
                "updated_at_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return out


def get_latest_session_id() -> str | None:
    items = list_sessions(limit=1)
    return items[0]["id"] if items else None


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
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            text = " ".join(" ".join(parts).split())
            if text:
                return text if len(text) <= 60 else text[:57] + "..."
    return "(no user message)"
