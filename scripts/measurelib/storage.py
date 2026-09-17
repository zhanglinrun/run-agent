"""Measure JSONL session storage.

Covers batch populate, individual appends, warm paginated reads, branch forks,
a brand-new reader's first read, and event-loop lag observed while a large
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

from run_agent_coding.jsonl_storage import SessionWriter
from run_agent_core.messages import UserMessage
from run_agent_core.session.entries import MessageEntry
from run_agent_core.session.storage import JsonlSessionStorage

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


async def populate(writer: SessionWriter, count: int) -> tuple[float, str]:
    """Batch-append a whole history and report the head it produced."""

    async def work() -> object:
        entries = [message_entry(index) for index in range(count)]
        return await writer.append_entries(entries, expected_head=None, token=writer.token)

    elapsed, receipt = await timed(work)
    return elapsed, getattr(receipt, "head_id", "") or ""


async def append_samples(writer: SessionWriter, head: str, first_index: int) -> list[float]:
    """Time a few single appends onto an already long history."""
    samples = []
    for index in range(first_index, first_index + APPEND_SAMPLES):
        started = time.perf_counter()
        receipt = await writer.append_entries(
            [message_entry(index)], token=writer.token, expected_head=head
        )
        samples.append((time.perf_counter() - started) * 1000)
        head = receipt.head_id or head
    return samples


async def paginated_read(writer: SessionWriter) -> float:
    """Read the whole history in pages."""
    after, pages = 0, 0
    started = time.perf_counter()
    while pages < 10_000:
        page = await writer.read_entries(after_seq=after, limit=PAGE_LIMIT)
        pages += 1
        if page.next_seq is None or not page.entries:
            break
        after = page.next_seq
    return (time.perf_counter() - started) * 1000


async def fresh_reader_read(path: Path) -> float:
    """Open a new reader and take its first read; the page cache is warm."""
    started = time.perf_counter()
    await JsonlSessionStorage(path).read_all()
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


async def measure_under_write_load(writer: SessionWriter, size: int) -> tuple[float, str, dict]:
    """Batch-append while sampling loop lag, and report the head it produced."""
    lags: list[float] = []
    watcher = asyncio.create_task(watch_lag(lags))
    populate_ms, head = await populate(writer, size)
    watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher
    return populate_ms, head, lag_summary(lags)


async def measure(size: int, root: Path) -> dict:
    """Measure one history size end to end."""
    path = root / f"{size}.jsonl"
    writer = SessionWriter(JsonlSessionStorage(path), "s", owner_id="bench")
    populate_ms, head, lag = await measure_under_write_load(writer, size)
    appends = await append_samples(writer, head, size)
    read_ms = await paginated_read(writer)
    marker = message_entry(size + APPEND_SAMPLES)
    marker = marker.model_copy(update={"parent_id": head or None, "id": "fork-marker"})
    fork_ms, _ = await timed(
        lambda: writer.fork(head or None, token=writer.token, entries=(marker,))
    )
    await writer.aclose()
    return {
        "entries": size,
        "populate_ms": populate_ms,
        "append_ms": appends,
        "paginated_read_ms": read_ms,
        "fork_ms": fork_ms,
        "fresh_connection_read_ms": await fresh_reader_read(path),
        "bytes": path.stat().st_size,
        "write_load_loop_lag": lag,
    }


async def run(sizes: tuple[int, ...], workdir: Path, repo_root: Path) -> dict:
    """Measure every requested size in one throwaway directory."""
    samples = [await measure(size, workdir) for size in sizes]
    return {
        "schema": "run.storage-measurements.v2",
        "revision": revision(repo_root),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "conditions": (
            "Append-only JSONL with a per-session file lock, Windows local filesystem, "
            "in-process. The fresh-reader measurement warms the OS page cache, so it is not a "
            "cold-cache measurement. Loop lag is sampled at ~1kHz while the batch populate runs "
            "and is reported as a distribution rather than a single figure. Synthetic sequential "
            "history; no claim about model or tool performance."
        ),
        "samples": samples,
    }


def measure_all(sizes: tuple[int, ...], repo_root: Path) -> dict:
    """Run the measurement in a temporary working directory."""
    with tempfile.TemporaryDirectory(prefix="run-storage-measure-") as directory:
        return asyncio.run(run(sizes, Path(directory), repo_root))
