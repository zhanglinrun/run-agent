"""S08: interactive, print, Gateway and Eval leave no product JSONL behind.

The four entry points share one working root here, then a single audit runs over
all of it. Two things must hold: no product JSONL state file may exist anywhere,
and the call ledger and spans must be queryable from SQLite instead.

The one legitimate ``*.jsonl`` in the repository is the evaluation task manifest
(``evals/coding/smoke/tasks.jsonl``), which is an input, not product state. This
test audits generated state directories, so that file is out of scope by
construction rather than by an exclusion list.
"""

import asyncio
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_evaluation_sqlite import EvaluatedProvider, settings
from tests.redesign.test_gateway_runtime import eventually, released, submit

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.terminal import Terminal
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import FrozenTask
from run_agent_evals.runner import EvaluationRunner
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.scheduler import GatewayScheduler


async def drive_interactive_and_print(opts) -> None:
    """Both terminal front ends must run through the one shared application."""
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        with create_pipe_input() as pipe:
            terminal = Terminal(
                app,
                console=Console(file=StringIO()),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            settled = asyncio.Event()
            render = terminal.render

            def observe(event) -> None:
                render(event)
                if isinstance(event, AgentSettledEvent):
                    settled.set()

            terminal.render = observe
            running = asyncio.create_task(terminal.run())
            pipe.send_text("interactive task\r")
            await asyncio.wait_for(settled.wait(), 15)
            pipe.send_text("/quit\r")
            await asyncio.wait_for(running, 15)
        events = [event async for event in app.prompt("print task")]
        assert events[-1].status == "succeeded"


async def drive_gateway(opts, workspace: Path) -> None:
    """One task through the real gateway: admit, schedule, complete, release."""
    async with await SqliteDatabase.open(opts.paths.database_path) as database:
        repository = GatewayRepository(database)
        await repository.initialize()
        owner = await repository.acquire_owner("s08-host")
        host = GatewayCodingRuntime(
            repository, owner, opts, provider_factory=lambda _: ReplyProvider()
        )
        scheduler = GatewayScheduler(repository, owner, CodingAssignmentRunner(host))
        receipt = await repository.admit(owner, submit(workspace, "gateway-1"), model="test")
        await scheduler.start()
        try:
            await eventually(lambda: released(repository, receipt.task_id))
            assert not scheduler.errors and scheduler.failure is None
        finally:
            await scheduler.shutdown()
            await repository.release_owner(owner)


async def drive_eval(tmp_path: Path, monkeypatch) -> dict:
    """One evaluation trial through the real SQLite-backed application."""
    monkeypatch.setattr(
        "run_agent_coding.session.create_model_provider", lambda *a, **k: EvaluatedProvider()
    )
    fixture = tmp_path / "eval-fixture"
    fixture.mkdir()
    task = FrozenTask("case", fixture, "answer", ((sys.executable, "-c", "pass"),))
    executor = CodingTaskExecutor(tmp_path / "eval-state", provider_settings=settings())
    trial = await EvaluationRunner(tmp_path / "eval-results").run_trial(
        task, executor, candidate_id="baseline", seed=0
    )
    assert trial.status == "passed"
    return trial.metadata


async def test_all_four_entry_points_generate_no_product_jsonl(tmp_path, monkeypatch):
    opts = options(tmp_path)
    await drive_interactive_and_print(opts)
    await drive_gateway(opts, tmp_path)
    metadata = await drive_eval(tmp_path, monkeypatch)

    written = sorted(path.name for path in tmp_path.rglob("*.jsonl"))
    assert written == [], f"product JSONL state files were written: {written}"

    with sqlite3.connect(metadata["database"]) as connection:
        streams = connection.execute(
            "SELECT stream, count(*) FROM observations GROUP BY stream"
        ).fetchall()
    assert streams, "the call ledger and spans must be queryable from SQLite"
    assert any(count >= 2 for _, count in streams), streams
    assert any(stream.startswith("calls:") for stream, _ in streams), streams


@pytest.mark.parametrize("removed", ["provider_calls", "trace_events"])
def test_no_removed_jsonl_style_table_exists(tmp_path, removed):

    async def inspect() -> list[str]:
        async with await SqliteDatabase.open(tmp_path / "s08.sqlite3") as database:
            return await database.run(
                lambda connection: [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
            )

    names = asyncio.run(inspect())
    assert "observations" in names
    assert removed not in names


def test_no_jsonl_reader_writer_adapter_or_rpc_survives():
    """S08 also forbids keeping the removed format's code paths around."""
    repo = Path(__file__).resolve().parents[2]
    sources = [
        *sorted((repo / "src").rglob("*.py")),
        *sorted((repo / "extensions").rglob("*.py")),
    ]
    for name in ("session/jsonl.py", "rpc.py", "stdin_jsonl.py"):
        matches = [str(p.relative_to(repo)) for p in sources if str(p).endswith(name)]
        assert matches == [], f"{name} still exists: {matches}"
    for anchor in ("JsonlSessionStorage", "JsonlRecorder", "stdin_jsonl"):
        offenders = [
            str(path.relative_to(repo))
            for path in sources
            if anchor in path.read_text(encoding="utf-8", errors="replace")
        ]
        assert offenders == [], f"{anchor} still referenced in {offenders}"
