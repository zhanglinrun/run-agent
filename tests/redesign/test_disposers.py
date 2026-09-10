import asyncio
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionError
from run_agent_coding.extensions.disposers import DisposerOwner


async def test_reload_and_close_dispose_each_generation_once_in_reverse_order(tmp_path):
    extension = tmp_path / "owned.py"
    extension.write_text(
        """
from pathlib import Path
def setup(api):
    async def one():
        with Path(__file__).with_suffix(".log").open("a") as stream: stream.write("one\\n")
    async def two():
        with Path(__file__).with_suffix(".log").open("a") as stream: stream.write("two\\n")
    api.register_disposer(one)
    api.register_disposer(two)
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(extension,))
    app = await CodingApplication.open(opts, provider=ReplyProvider())
    await app.start()
    old = app.session.extension_runtime._extensions[0].api
    assert not extension.with_suffix(".log").exists()
    await app.command("/reload")
    assert extension.with_suffix(".log").read_text().splitlines() == ["two", "one"]
    with pytest.raises(ExtensionError):
        old.register_disposer(lambda: None)
    await app.aclose()
    await app.aclose()
    assert extension.with_suffix(".log").read_text().splitlines() == ["two", "one"] * 2


async def test_failed_setup_cleanup_runs_before_publishing_surviving_sources(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        """
from pathlib import Path
def setup(api):
    async def cleanup():
        Path(__file__).with_suffix(".closed").touch()
    api.register_disposer(cleanup)
    raise ValueError("setup failure")
""",
        encoding="utf-8",
    )
    good = tmp_path / "good.py"
    good.write_text(
        """
from pathlib import Path
def setup(api):
    def started(event, context):
        assert Path(__file__).with_name("bad.closed").exists()
        Path(__file__).with_suffix(".started").touch()
    api.on("session_start", started)
""",
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(bad, good))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        assert bad.with_suffix(".closed").exists()
        assert good.with_suffix(".started").exists()
        assert len(app.session.extension_runtime._extensions) == 1


async def test_disposer_failure_does_not_skip_earlier_acquisitions_or_other_sources():
    owner = DisposerOwner(timeout=0.02)
    calls = []

    async def earlier():
        calls.append("earlier")

    async def broken():
        raise OSError("close failed")

    async def other():
        calls.append("other")

    owner.register("one", earlier)
    owner.register("one", broken)
    owner.register("two", other)
    owner.retire()
    assert await owner.drain() == 0
    assert sorted(calls) == ["earlier", "other"]
    assert owner.errors == ["one: OSError: close failed"]
    assert await owner.drain() == 0
    assert len(calls) == 2


async def test_uncooperative_disposer_remains_owned_and_visible_until_it_exits():
    owner = DisposerOwner(timeout=0.01)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def earlier():
        calls.append("earlier")

    async def slow():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    owner.register("slow", earlier)
    owner.register("slow", slow)
    owner.retire()
    assert await owner.drain() == 1
    assert entered.is_set() and cancelled.is_set()
    assert len(owner._tasks) == 1
    release.set()
    assert await owner.drain() == 0
    assert calls == ["earlier"] and owner.errors == []


async def test_cancelled_disposer_does_not_skip_sibling_cleanup():
    owner = DisposerOwner(timeout=0.01)
    calls = []

    async def earlier():
        calls.append("earlier")

    async def cancelled():
        await asyncio.Event().wait()

    owner.register("one", earlier)
    owner.register("one", cancelled)
    owner.retire()
    assert await owner.drain() == 0
    assert calls == ["earlier"]
    assert owner.errors == ["one: cleanup callback was cancelled"]
