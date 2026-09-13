"""Duplicate suppression that survives a restart, for Feishu message and card events.

Feishu delivers at least once: a WebSocket reconnect replays the backlog and a webhook
retries until acknowledged. hermes keeps the ids it has seen in a JSON file with a
timestamp per id, so a restart does not re-answer messages it already answered. Card
actions get a short in-memory window, since a button can be pressed twice.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

CARD_ACTION_DEDUP_TTL_SECONDS = 15 * 60


class SeenMessages:
    """A bounded, TTL-limited set of message ids persisted as ``{id: seen_at}``."""

    def __init__(
        self,
        path: Path,
        *,
        size: int = 2048,
        ttl_seconds: float = 24 * 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.size = max(1, size)
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def first_sight(self, message_id: str) -> bool:
        """Record the id; False when it was already seen inside the TTL."""
        if not message_id:
            return True
        now = self._clock()
        with self._lock:
            seen_at = self._seen.get(message_id)
            if seen_at is not None and (self.ttl_seconds <= 0 or now - seen_at < self.ttl_seconds):
                return False
            self._seen[message_id] = now
            self._seen.move_to_end(message_id)
            while len(self._seen) > self.size:
                self._seen.popitem(last=False)
            self._persist_locked()
            return True

    def persist(self) -> None:
        with self._lock:
            self._persist_locked()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            logger.warning("[feishu] could not read dedup state %s", self.path, exc_info=True)
            return
        raw = payload.get("message_ids", {}) if isinstance(payload, dict) else {}
        now = self._clock()
        entries: dict[str, float] = {}
        if isinstance(raw, list):
            # An older layout stored a bare list; treat those as fresh for one cycle.
            entries = {str(item): now for item in raw if str(item).strip()}
        elif isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                try:
                    entries[key] = float(value)
                except (TypeError, ValueError):
                    continue
        valid = {
            key: stamp
            for key, stamp in entries.items()
            if self.ttl_seconds <= 0 or now - stamp < self.ttl_seconds
        }
        for key in sorted(valid, key=lambda k: valid[k])[-self.size :]:
            self._seen[key] = valid[key]

    def _persist_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"message_ids": dict(self._seen)}
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            logger.warning("[feishu] could not persist dedup state %s", self.path, exc_info=True)


class ActionDedup:
    """An in-memory TTL window for card-action tokens."""

    def __init__(
        self,
        *,
        ttl_seconds: float = CARD_ACTION_DEDUP_TTL_SECONDS,
        size: int = 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.size = max(1, size)
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, float] = OrderedDict()

    def first_sight(self, token: str) -> bool:
        now = self._clock()
        with self._lock:
            for key in [k for k, stamp in self._seen.items() if now - stamp >= self.ttl_seconds]:
                del self._seen[key]
            if token in self._seen:
                return False
            self._seen[token] = now
            while len(self._seen) > self.size:
                self._seen.popitem(last=False)
            return True


__all__ = ["CARD_ACTION_DEDUP_TTL_SECONDS", "ActionDedup", "SeenMessages"]
