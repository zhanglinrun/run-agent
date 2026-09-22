"""Two real spawned processes race the same ``expected_head`` compare-and-append.

Moved out of ``test_jsonl_sessions.py`` unchanged in substance: the spawn pair is a
slow test and the plan asks for it to run exactly once per session.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path

from run_agent_core.session.contracts import SessionConflict
from run_agent_core.session.entries import CustomEntry
from run_agent_core.session.storage import JsonlSessionStorage


def _competing_append(
    path: str,
    parent_id: str,
    entry_id: str,
    ready: multiprocessing.synchronize.Event,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Child process: report ``conflict`` or ``applied`` for one CAS attempt."""

    async def append() -> None:
        ready.set()
        start.wait()
        entry = CustomEntry(id=entry_id, parent_id=parent_id, namespace="race", data={})
        try:
            await JsonlSessionStorage(path).compare_and_append((entry,), parent_id)
        except SessionConflict:
            results.put("conflict")
        else:
            results.put("applied")

    asyncio.run(append())


async def test_compare_and_append_has_one_cross_process_winner(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    first = CustomEntry(namespace="race", data={"root": True})
    await JsonlSessionStorage(path).compare_and_append((first,), None)
    context = multiprocessing.get_context("spawn")
    ready = (context.Event(), context.Event())
    start = context.Event()
    results = context.Queue()
    processes = tuple(
        context.Process(
            target=_competing_append,
            args=(str(path), first.id, f"child-{index}", ready[index], start, results),
        )
        for index in range(2)
    )
    for process in processes:
        process.start()
    for event in ready:
        assert event.wait(10)
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    # Exactly one winner: the loser observed the winner's head through the file lock.
    assert sorted(results.get(timeout=2) for _ in processes) == ["applied", "conflict"]
    entries = await JsonlSessionStorage(path).read_all()
    assert [entry.id for entry in entries] in (
        [first.id, "child-0"],
        [first.id, "child-1"],
    )
