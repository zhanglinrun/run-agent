"""Heartbeats: scheduled prompts that wake a chat's session on their own.

A heartbeat is a durable job bound to a chat: a prompt, an interval (or one due time), and
the chat's source so the reply lands where the job was created. The scheduler polls the job
file, and when a job is due it injects a synthetic internal message through the adapter, so
the turn runs on the chat's normal session with the usual serialization and the reply goes
back through the usual send path. Failures are recorded on the job rather than dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from run_agent_gateway.session import SessionSource

logger = logging.getLogger(__name__)

WakeHandler = Callable[["HeartbeatJob"], Awaitable[None]]


@dataclass(slots=True)
class HeartbeatJob:
    job_id: str
    session_key: str
    source: SessionSource
    prompt: str
    interval_seconds: float | None
    next_run_at: float
    created_at: float
    last_run_at: float | None = None
    last_error: str = ""
    runs: int = 0
    enabled: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["source"] = self.source.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> HeartbeatJob:
        source = data.get("source")
        interval = data.get("interval_seconds")
        return cls(
            job_id=str(data["job_id"]),
            session_key=str(data["session_key"]),
            source=SessionSource.from_dict(source if isinstance(source, dict) else {}),
            prompt=str(data["prompt"]),
            interval_seconds=None if interval is None else float(str(interval)),
            next_run_at=float(str(data["next_run_at"])),
            created_at=float(str(data.get("created_at", 0.0))),
            last_run_at=(
                None if data.get("last_run_at") is None else float(str(data.get("last_run_at")))
            ),
            last_error=str(data.get("last_error", "")),
            runs=int(str(data.get("runs", 0))),
            enabled=bool(data.get("enabled", True)),
            metadata={str(k): str(v) for k, v in dict(data.get("metadata") or {}).items()},  # type: ignore[call-overload]
        )

    def describe(self) -> str:
        if self.interval_seconds is None:
            cadence = "一次"
        else:
            cadence = f"每 {self.interval_seconds / 60:g} 分钟"
        wait = max(0.0, self.next_run_at - time.time())  # display only
        status = "" if self.enabled else "（已暂停）"
        tail = f"，上次出错：{self.last_error}" if self.last_error else ""
        return (
            f"{self.job_id}  {cadence}，{wait / 60:.1f} 分钟后执行，已运行 {self.runs} 次{status}"
            f"：{self.prompt[:60]}{tail}"
        )


class HeartbeatStore:
    """Job list persisted as one JSON document."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self._jobs: dict[str, HeartbeatJob] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        document = json.loads(self.path.read_text(encoding="utf-8"))
        for raw in document.get("jobs", []) if isinstance(document, dict) else []:
            if isinstance(raw, dict):
                job = HeartbeatJob.from_dict(raw)
                self._jobs[job.job_id] = job

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "jobs": [job.to_dict() for job in self._jobs.values()]}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def add(
        self,
        session_key: str,
        source: SessionSource,
        prompt: str,
        *,
        interval_seconds: float | None,
        first_run_at: float | None = None,
    ) -> HeartbeatJob:
        if not prompt.strip():
            raise ValueError("Heartbeat prompt must not be empty")
        if interval_seconds is not None and interval_seconds < 60:
            raise ValueError("Heartbeat interval must be at least one minute")
        now = self.clock()
        first = first_run_at if first_run_at is not None else now + (interval_seconds or 0)
        job = HeartbeatJob(
            uuid4().hex[:8], session_key, source, prompt.strip(), interval_seconds, first, now
        )
        self._jobs[job.job_id] = job
        self.save()
        return job

    def remove(self, job_id: str) -> bool:
        removed = self._jobs.pop(job_id, None) is not None
        if removed:
            self.save()
        return removed

    def get(self, job_id: str) -> HeartbeatJob | None:
        return self._jobs.get(job_id)

    def for_session(self, session_key: str) -> list[HeartbeatJob]:
        return [job for job in self._jobs.values() if job.session_key == session_key]

    def due(self, now: float) -> list[HeartbeatJob]:
        return sorted(
            (j for j in self._jobs.values() if j.enabled and j.next_run_at <= now),
            key=lambda j: j.next_run_at,
        )

    def complete(self, job: HeartbeatJob, *, error: str = "") -> None:
        now = self.clock()
        job.last_run_at = now
        job.runs += 1
        job.last_error = error
        if job.interval_seconds is None:
            self._jobs.pop(job.job_id, None)
        else:
            # Schedule from the planned time so a slow turn does not drift the cadence,
            # but never pile up several missed runs.
            job.next_run_at = max(job.next_run_at + job.interval_seconds, now + 1)
        self.save()

    def __len__(self) -> int:
        return len(self._jobs)


class HeartbeatScheduler:
    def __init__(
        self,
        store: HeartbeatStore,
        wake: WakeHandler,
        *,
        poll_seconds: float = 15.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self._wake = wake
        self._poll = max(0.05, poll_seconds)
        self._clock = clock or store.clock
        self._task: asyncio.Task[None] | None = None
        self._running: set[str] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="gateway-heartbeat")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("heartbeat tick failed")
            await asyncio.sleep(self._poll)

    async def tick(self) -> int:
        """Fire every due job once; returns how many were started."""
        fired = 0
        for job in self.store.due(self._clock()):
            if job.job_id in self._running:
                continue
            self._running.add(job.job_id)
            fired += 1
            try:
                await self._wake(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("heartbeat %s failed", job.job_id)
                self.store.complete(job, error=str(exc)[:200])
            else:
                self.store.complete(job)
            finally:
                self._running.discard(job.job_id)
        return fired


__all__ = ["HeartbeatJob", "HeartbeatScheduler", "HeartbeatStore", "WakeHandler"]
