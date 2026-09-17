"""Interactive, print, Gateway and Eval persist JSONL rather than SQLite."""

import asyncio
import json
import sys
from io import StringIO
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_evaluation_sqlite import EvaluatedProvider, settings
from tests.redesign.test_gateway_runner import FakeAdapter
from tests.redesign.test_gateway_runner import config as gateway_config

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.terminal import Terminal
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.models import FrozenTask
from run_agent_evals.runner import EvaluationRunner
from run_agent_gateway.run import GatewayRunner


async def drive_interactive_and_print(opts) -> None:
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


async def drive_gateway(opts) -> None:
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


async def test_all_four_entry_points_write_jsonl_not_sqlite(tmp_path, monkeypatch):
    opts = options(tmp_path)
    await drive_interactive_and_print(opts)
    await drive_gateway(opts)
    metadata = await drive_eval(tmp_path, monkeypatch)

    sqlite_files = sorted(path.name for path in tmp_path.rglob("*.sqlite3"))
    assert sqlite_files == [], sqlite_files
    session_files = list((tmp_path / ".run" / "sessions").glob("*.jsonl"))
    assert any(path.name != "index.jsonl" for path in session_files)
    observations = Path(metadata["observations"])
    assert observations.is_file()
    body = observations.read_text(encoding="utf-8")
    assert metadata["call_stream"] in body
    rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert any(row.get("stream", "").startswith("calls:") for row in rows)
