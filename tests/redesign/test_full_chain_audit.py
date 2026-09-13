"""Interactive, print, Gateway and Eval leave no product JSONL behind.

The four entry points share one working root here, then a single audit runs over
all of it. Two things must hold: no product JSONL state file may exist anywhere,
and the call ledger and spans must be queryable from SQLite instead. Evaluation
task manifests are inputs, not product state, and live outside the audited
state directories by construction.
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
from tests.redesign.test_gateway_runner import FakeAdapter
from tests.redesign.test_gateway_runner import config as gateway_config

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_coding.terminal import Terminal
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import FrozenTask
from run_agent_evals.runner import EvaluationRunner
from run_agent_gateway.run import GatewayRunner


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
    """One chat message through the real gateway runner: session, agent, reply."""
    adapter = FakeAdapter()
    gateway = GatewayRunner(
        gateway_config(), opts, adapter, provider_factory=lambda: ReplyProvider()
    )
    await gateway.start()
    try:
        await adapter.deliver("gateway-1")
        await adapter.wait_idle()
        assert adapter.sent[-1][1] == "reply: gateway-1"
    finally:
        await gateway.stop()


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
    """The removed format's code paths must not survive either."""
    repo = Path(__file__).resolve().parents[2]
    sources = [
        *sorted((repo / "src").rglob("*.py")),
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
