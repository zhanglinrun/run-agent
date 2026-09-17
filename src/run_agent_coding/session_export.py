"""Portable HTML reports; JSONL session trees are the recovery format."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path

from run_agent_core.messages import message_text
from run_agent_core.session import MessageEntry, SessionEntry


def export_session_html(
    entries: Sequence[SessionEntry], output_path: Path, *, title: str = "Run Agent Session"
) -> Path:
    cards = []
    for entry in entries:
        label = entry.message.role if isinstance(entry, MessageEntry) else entry.type
        text = message_text(entry.message) if isinstance(entry, MessageEntry) else ""
        details = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, indent=2)
        parent = (
            f'<a href="#{html.escape(entry.parent_id, quote=True)}">parent</a>'
            if entry.parent_id
            else "root"
        )
        cards.append(
            f'<article id="{html.escape(entry.id, quote=True)}"><h2>{html.escape(label)}</h2>'
            f"<small>#{entry.seq or 'pending'} · {parent}</small><pre>{html.escape(text)}</pre>"
            f"<details><summary>Details</summary><pre>{html.escape(details)}</pre></details></article>"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        '<!doctype html><html lang="zh"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{max-width:960px;margin:40px auto;padding:0 24px;font:16px/1.6 system-ui;"
        "background:#101820;color:#e5e9ee}article{border-top:1px solid #405060;padding:16px 0}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#80bdff}small{color:#a0b0c0}"
        "</style>"
        f"<h1>{html.escape(title)}</h1>{''.join(cards)}</html>",
        encoding="utf-8",
    )
    return output_path
