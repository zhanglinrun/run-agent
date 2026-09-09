"""Measure the SQLite repository without involving a model or frontend."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

from run_agent_coding.storage.sessions import SqliteSessionRepository
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import UserMessage
from run_agent_core.session.entries import MessageEntry


async def measure(size: int, root: Path) -> dict[str, object]:
    database = await SqliteDatabase.open(root / f"{size}.sqlite3")
    repository = SqliteSessionRepository(database)
    try:
        await repository.create_session(
            cwd=root, principal_id="benchmark", model="mock", session_id="s"
        )
        token = await repository.claim("s", owner_id="benchmark", run_id="run")
        entries = [
            MessageEntry(
                id=f"entry-{index}",
                parent_id=f"entry-{index - 1}" if index else None,
                message=UserMessage(content="Repository task evidence. " * 12),
            )
            for index in range(size)
        ]
        start = perf_counter()
        await repository.append_entries(entries, token=token, expected_head=None)
        populate_ms = (perf_counter() - start) * 1000
        samples = []
        for index in range(3):
            entry = MessageEntry(
                id=f"entry-{size + index}",
                parent_id=f"entry-{size + index - 1}",
                message=UserMessage(content="Follow-up evidence."),
            )
            start = perf_counter()
            await repository.append_entries([entry], token=token, expected_head=entry.parent_id)
            samples.append((perf_counter() - start) * 1000)
        loop_lag = []
        stopped = asyncio.Event()

        async def heartbeat():
            while not stopped.is_set():
                start = perf_counter()
                await asyncio.sleep(0.005)
                loop_lag.append(max(0, (perf_counter() - start) * 1000 - 5))

        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        start = perf_counter()
        count, after_seq = 0, 0
        while True:
            page = await repository.read_entries("s", after_seq=after_seq)
            count += len(page.entries)
            if page.next_seq is None:
                break
            after_seq = page.next_seq
        read_ms = (perf_counter() - start) * 1000
        stopped.set()
        await ticker
        assert count == size + 3
        start = perf_counter()
        await repository.fork_branch(
            token=token, branch_id="fork", at_entry_id=f"entry-{size // 2}"
        )
        fork_ms = (perf_counter() - start) * 1000
        return {
            "entries": size,
            "populate_ms": populate_ms,
            "append_ms": samples,
            "warm_paginated_read_ms": read_ms,
            "fork_ms": fork_ms,
            "read_event_loop_lag_ms": loop_lag,
        }
    finally:
        await database.aclose()


async def run(destination: Path, sizes: list[int]) -> None:
    with tempfile.TemporaryDirectory(prefix="run-sqlite-benchmark-") as directory:
        samples = [await measure(size, Path(directory)) for size in sizes]
    report = {
        "schema": "run.storage-benchmark.v1",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "worktree_modified": bool(
            subprocess.check_output(["git", "diff", "--name-only"], text=True)
        ),
        "conditions": "Synthetic sequential message history; WAL synchronous=FULL; "
        "single worker; batch population; 3 individual append samples; "
        "warm 1000-entry pages; event-loop lag during reading only. "
        "Measures repository components, not the integrated application.",
        "samples": samples,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for sample in samples:
        print(f"{sample['entries']} entries: append_ms={sample['append_ms']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10_000, 100_000])
    args = parser.parse_args()
    if any(size < 2 for size in args.sizes):
        parser.error("sizes must be at least 2")
    asyncio.run(run(args.output, args.sizes))
