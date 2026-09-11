"""Measure SQLite session storage.

Covers batch populate, individual appends, warm paginated reads, branch forks,
a brand-new connection's first read, and event-loop lag observed while a large
write is in flight.
"""

from __future__ import annotations

import asyncio
import platform
import statistics
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import UserMessage
from run_agent_core.session.entries import MessageEntry

APPEND_SAMPLES = 3
PAGE_LIMIT = 1_000
LAG_INTERVAL_SECONDS = 0.001
DEFAULT_SIZES = (1_000, 10_000, 100_000)


def message_entry(index: int) -> MessageEntry:
    """One representative history entry: a user turn of realistic size."""
    return MessageEntry(
        id=f"entry-{index}",
        parent_id=f"entry-{index - 1}" if index else None,
        message=UserMessage(content="Repository task evidence. " * 12),
    )


def revision(root: Path) -> str:
    """The commit the measurement was taken at."""
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


async def watch_lag(samples: list[float]) -> None:
    """Sample the event loop's scheduling delay until the task is cancelled."""
    previous = time.perf_counter()
    while True:
        await asyncio.sleep(LAG_INTERVAL_SECONDS)
        now = time.perf_counter()
        samples.append((now - previous - LAG_INTERVAL_SECONDS) * 1000)
        previous = now


async def timed(work: Callable[[], Awaitable[object]]) -> tuple[float, object]:
    """Run one awaited operation and report both its duration and its result."""
    started = time.perf_counter()
    result = await work()
    return (time.perf_counter() - started) * 1000, result


async def populate(repository: SqliteSessionRepository, token, count: int) -> tuple[float, str]:
    """Batch-append a whole history and report the head it produced."""

    async def work() -> object:
        return await repository.append_entries(
            [message_entry(index) for index in range(count)], token=token, expected_head=None
        )

    elapsed, receipt = await timed(work)
    return elapsed, getattr(receipt, "head_id", "") or ""


async def append_samples(
    repository: SqliteSessionRepository, token, head: str, first_index: int
) -> list[float]:
    """Time a few single appends onto an already long history."""
    samples = []
    for index in range(first_index, first_index + APPEND_SAMPLES):
        started = time.perf_counter()
        receipt = await repository.append_entries(
            [message_entry(index)], token=token, expected_head=head
        )
        samples.append((time.perf_counter() - started) * 1000)
        head = receipt.head_id or head
    return samples


async def paginated_read(repository: SqliteSessionRepository, session_id: str) -> float:
    """Read the whole history in pages."""
    after, pages = 0, 0
    started = time.perf_counter()
    while pages < 10_000:
        page = await repository.read_entries(session_id, after_seq=after, limit=PAGE_LIMIT)
        pages += 1
        if page.next_seq is None or not page.entries:
            break
        after = page.next_seq
    return (time.perf_counter() - started) * 1000


async def fresh_connection_read(path: Path, session_id: str) -> float:
    """Open a new connection and take its first read; the page cache is warm."""
    started = time.perf_counter()
    async with await SqliteDatabase.open(path) as database:
        await SqliteSessionRepository(database).read_entries(session_id, limit=1)
    return (time.perf_counter() - started) * 1000


def lag_summary(samples: list[float]) -> dict:
    """Report the lag distribution and its sample count, never a single figure."""
    if not samples:
        return {"samples": 0, "max_ms": None, "p95_ms": None, "mean_ms": None}
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "max_ms": ordered[-1],
        "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
        "mean_ms": statistics.fmean(ordered),
    }


async def measure_under_write_load(
    repository: SqliteSessionRepository, token, size: int
) -> tuple[float, str, dict]:
    """Batch-append while sampling loop lag, and report the head it produced."""
    lags: list[float] = []
    watcher = asyncio.create_task(watch_lag(lags))
    populate_ms, head = await populate(repository, token, size)
    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher
    return populate_ms, head, lag_summary(lags)


async def measure(size: int, root: Path) -> dict:
    """Measure one history size end to end."""
    path = root / f"{size}.sqlite3"
    async with await SqliteDatabase.open(path) as database:
        repository = SqliteSessionRepository(database)
        await repository.create_session(
            cwd=root, principal_id="bench", model="bench", session_id="s"
        )
        token = await repository.claim("s", owner_id="bench", run_id="run")
        populate_ms, head, lag = await measure_under_write_load(repository, token, size)
        appends = await append_samples(repository, token, head, size)
        read_ms = await paginated_read(repository, "s")
        fork_ms, _ = await timed(
            lambda: repository.fork_branch(
                token=token, branch_id="forked", at_entry_id=head or None
            )
        )
    return {
        "entries": size,
        "populate_ms": populate_ms,
        "append_ms": appends,
        "paginated_read_ms": read_ms,
        "fork_ms": fork_ms,
        "fresh_connection_read_ms": await fresh_connection_read(path, "s"),
        "bytes": path.stat().st_size,
        "write_load_loop_lag": lag,
    }


async def run(sizes: tuple[int, ...], workdir: Path, repo_root: Path) -> dict:
    """Measure every requested size in one throwaway directory."""
    samples = [await measure(size, workdir) for size in sizes]
    return {
        "schema": "run.storage-measurements.v1",
        "revision": revision(repo_root),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "conditions": (
            "SQLite WAL, synchronous=FULL, single worker, Windows local filesystem, in-process. "
            "The fresh-connection read warms the OS page cache, so it is not a cold-cache "
            "measurement. Loop lag is sampled at ~1kHz while the batch populate runs and is "
            "reported as a distribution rather than a single figure; the sample count is itself "
            "a signal, because the sampler only runs when the loop is free, so fewer samples over "
            "a longer populate means the loop was blocked for longer. Synthetic sequential "
            "history; no claim about model or tool performance."
        ),
        "samples": samples,
    }


def measure_all(sizes: tuple[int, ...], repo_root: Path) -> dict:
    """Run the measurement in a temporary working directory."""
    with tempfile.TemporaryDirectory(prefix="run-storage-measure-") as directory:
        return asyncio.run(run(sizes, Path(directory), repo_root))
