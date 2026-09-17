"""Chat-to-session mapping: keys, persistence and the reset policy."""

import json
from datetime import datetime, timedelta

import pytest

from run_agent_gateway.config import SessionResetPolicy
from run_agent_gateway.session import SessionSource, SessionStore, build_session_key


def source(**overrides):
    base = {"platform": "feishu", "chat_id": "chat", "chat_type": "dm", "user_id": "alice"}
    return SessionSource(**{**base, **overrides})


def _latest(path, session_key: str) -> dict:
    payload = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("session_key") == session_key:
            payload = row
    assert payload is not None
    return payload


def test_session_keys_follow_the_hermes_rules():
    assert build_session_key(source()) == "feishu:dm:chat"
    assert build_session_key(source(thread_id="t1")) == "feishu:dm:chat:t1"
    group = source(chat_type="group")
    assert build_session_key(group) == "feishu:group:chat:alice"
    assert build_session_key(group, group_sessions_per_user=False) == "feishu:group:chat"
    thread = source(chat_type="group", thread_id="t1")
    assert build_session_key(thread) == "feishu:group:chat:t1"
    assert build_session_key(thread, thread_sessions_per_user=True) == "feishu:group:chat:t1:alice"


def test_store_persists_and_reloads_entries(tmp_path):
    path = tmp_path / "gateway" / "sessions.jsonl"
    store = SessionStore(path, SessionResetPolicy())
    entry = store.get_or_create("feishu:dm:chat", source(user_name="Alice"))
    assert store.get_or_create("feishu:dm:chat", source()).session_id == entry.session_id
    fresh = store.reset("feishu:dm:chat", source())
    assert fresh.session_id != entry.session_id
    payload = _latest(path, "feishu:dm:chat")
    assert payload["session_id"] == fresh.session_id
    reloaded = SessionStore(path, SessionResetPolicy())
    again = reloaded.get("feishu:dm:chat")
    assert again is not None and again.session_id == fresh.session_id
    assert again.origin.user_id == "alice"


def test_store_imports_legacy_sessions_json(tmp_path):
    json_path = tmp_path / "sessions.json"
    json_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "feishu:dm:chat": {
                        "session_key": "feishu:dm:chat",
                        "session_id": "imported-id",
                        "created_at": 1.0,
                        "updated_at": 2.0,
                        "origin": source().to_dict(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = SessionStore(tmp_path / "sessions.jsonl", SessionResetPolicy())
    imported = store.get("feishu:dm:chat")
    assert imported is not None and imported.session_id == "imported-id"
    reloaded = SessionStore(tmp_path / "sessions.jsonl", SessionResetPolicy())
    again = reloaded.get("feishu:dm:chat")
    assert again is not None and again.session_id == "imported-id"


def test_jsonl_row_wins_over_legacy_json(tmp_path):
    jsonl_path = tmp_path / "sessions.jsonl"
    store = SessionStore(jsonl_path, SessionResetPolicy())
    live = store.get_or_create("feishu:dm:chat", source())
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "feishu:dm:chat": {
                        "session_key": "feishu:dm:chat",
                        "session_id": "stale-json-id",
                        "created_at": 1.0,
                        "updated_at": 2.0,
                        "origin": source().to_dict(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reloaded = SessionStore(jsonl_path, SessionResetPolicy())
    again = reloaded.get("feishu:dm:chat")
    assert again is not None and again.session_id == live.session_id


@pytest.mark.parametrize(
    ("mode", "elapsed", "reason"),
    [
        ("none", timedelta(days=30), None),
        ("idle", timedelta(minutes=30), None),
        ("idle", timedelta(minutes=90), "idle"),
        ("daily", timedelta(hours=2), "daily"),
        ("both", timedelta(minutes=10), None),
    ],
)
def test_reset_policy_decides_when_a_chat_starts_over(tmp_path, mode, elapsed, reason):
    moment = datetime(2026, 9, 12, 5, 0, 0)
    store = SessionStore(
        tmp_path / "sessions.jsonl",
        SessionResetPolicy(mode=mode, idle_minutes=60, at_hour=4),
        now=lambda: moment,
    )
    entry = store.get_or_create("k", source())
    entry.updated_at = (moment - elapsed).timestamp()
    current = store.get_or_create("k", source())
    if reason is None:
        assert current.session_id == entry.session_id and not current.was_auto_reset
    else:
        assert current.session_id != entry.session_id
        assert current.was_auto_reset and current.auto_reset_reason == reason
