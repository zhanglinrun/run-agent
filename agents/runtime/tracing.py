"""Append-only runtime traces suitable for replay and evaluation evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from .contracts import EventType, new_id, utc_now
from .scope import current_workspace


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = ("api_key", "access_token", "refresh_token", "password", "secret")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[:=]\s*)([^\s,;]+)"
    ),
)


def _redact_string(value: str) -> str:
    text = _SECRET_PATTERNS[0].sub("[REDACTED]", value)
    return _SECRET_PATTERNS[1].sub(lambda match: match.group(1) + "[REDACTED]", text)


def _trace_root() -> Path:
    configured = os.environ.get("RUN_TRACE_DIR", "").strip()
    return Path(configured) if configured else current_workspace() / ".run" / "traces"


def _redact(value: Any, *, key: str = "") -> Any:
    normalized_key = key.lower()
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(_SENSITIVE_KEY_SUFFIXES):
        return "[REDACTED]"
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        value = _redact_string(value)
        if len(value) > 20_000:
            return value[:10_000] + f"\n[... {len(value) - 20_000} chars omitted ...]\n" + value[-10_000:]
    return value


@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    sequence: int
    timestamp: str
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TraceRecorder:
    """Write JSONL events immediately so interrupted runs still have evidence."""

    def __init__(
        self,
        *,
        session_id: str,
        model: str,
        enabled: bool | None = None,
        root: Path | None = None,
    ) -> None:
        env_enabled = os.environ.get("RUN_TRACE", "1").strip().lower() not in {"0", "false", "off", "no"}
        self.enabled = env_enabled if enabled is None else bool(enabled)
        self.session_id = session_id
        self.model = model
        self.root = root or _trace_root()
        self.run_id: str | None = None
        self.path: Path | None = None
        self._sequence = 0
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._finished = False

    def start_run(self, prompt: str, **metadata: Any) -> str | None:
        if not self.enabled:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = new_id("trace")
        self.path = self.root / f"{self.run_id}.jsonl"
        self._sequence = 0
        self._started_at = time.perf_counter()
        self._finished = False
        self.emit(
            EventType.RUN_STARTED,
            prompt=prompt,
            session_id=self.session_id,
            model=self.model,
            cwd=str(current_workspace()),
            **metadata,
        )
        self.emit(EventType.USER_MESSAGE, content=prompt)
        return self.run_id

    def emit(self, event_type: EventType | str, **payload: Any) -> None:
        if not self.enabled or self._finished or not self.run_id or not self.path:
            return
        with self._lock:
            self._sequence += 1
            event = TraceEvent(
                event_id=new_id("evt"),
                run_id=self.run_id,
                sequence=self._sequence,
                timestamp=utc_now(),
                type=event_type.value if isinstance(event_type, EventType) else str(event_type),
                payload=_redact(payload),
            )
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, default=str) + "\n")

    def finish_run(self, *, answer: str, tokens: dict[str, int], success: bool = True, error: str | None = None) -> None:
        if not self.run_id or self._finished:
            return
        duration_ms = (time.perf_counter() - self._started_at) * 1000 if self._started_at else 0.0
        self.emit(
            EventType.RUN_COMPLETED if success else EventType.RUN_FAILED,
            answer=answer,
            tokens=tokens,
            duration_ms=round(duration_ms, 3),
            success=success,
            error=error,
        )
        self._finished = True


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    return events


def trace_digest(events: list[dict[str, Any]]) -> str:
    raw = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
