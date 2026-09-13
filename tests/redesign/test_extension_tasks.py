import asyncio
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.extensions.api import ExtensionError
from run_agent_coding.extensions.loader import LoadedExtension
from run_agent_coding.host.contracts import StateChange, TaskSpec
from run_agent_coding.storage.tasks import TaskRejected


@pytest.fixture
def extension(tmp_path):
    path = tmp_path / "tasks.py"
    path.write_text("def setup(api): pass", encoding="utf-8")
    return path


async def completed(tasks, task_id):
    async with asyncio.timeout(3):
        while True:
            status = await tasks.status(task_id)
            if status.status in {"succeeded", "failed", "cancelled", "interrupted"}:
                return status
            await asyncio.sleep(0.005)


async def test_bounded_jobs_persist_descriptors_and_do_not_spawn_queued_coroutines(
    tmp_path,
    extension,
):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        release, started = asyncio.Event(), asyncio.Event()

        async def handler(payload, context):
            started.set()
            await release.wait()
            return payload

        runtime = app.session.extension_runtime
        runtime._extensions[0].api.register_task_handler("wait", handler)
        host = app.session.host_services
        host.tasks.concurrency, host.tasks.max_pending = 1, 2
        await app.start()
        tasks = context(app).services.tasks
        payload = {"value": "before"}
        first = await tasks.submit(TaskSpec("wait", payload))
        payload["value"] = "after"
        await started.wait()
        second = await tasks.submit(TaskSpec("wait", {}))
        assert (await tasks.status(second)).status == "queued"
        with pytest.raises(TaskRejected, match="capacity"):
            await tasks.submit(TaskSpec("wait", {}))
        assert len(host.tasks._running) == 1
        assert len(host.tasks._pending) == 1
        assert (await tasks.cancel(second)).status == "cancelled"
        release.set()
        assert (await completed(tasks, first)).result == {"value": "before"}
        assert not host.tasks._jobs


async def test_reload_cancels_running_and_queued_jobs_and_old_api_cannot_submit(
    tmp_path, extension
):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def wait(payload, task_context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        old_runtime = app.session.extension_runtime
        api = old_runtime._extensions[0].api
        api.register_task_handler("wait", wait)
        app.session.host_services.tasks.concurrency = 1
        await app.start()
        tasks = context(app).services.tasks
        first = await tasks.submit(TaskSpec("wait", {}))
        second = await tasks.submit(TaskSpec("wait", {}))
        await started.wait()
        await app.command("/reload")
        assert cleaned.is_set()
        fresh = context(app).services.tasks
        assert (await fresh.status(first)).status == "cancelled"
        assert (await fresh.status(second)).status == "cancelled"
        with pytest.raises(ExtensionError):
            await tasks.submit(TaskSpec("wait", {}))
        assert not app.session.host_services.tasks._jobs


async def test_noncooperative_task_is_reported_and_cannot_publish_after_retirement(
    tmp_path, extension
):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        started, release = asyncio.Event(), asyncio.Event()
        rejected = []

        async def stubborn(payload, task_context):
            state = task_context.services.scope().state
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            try:
                await state.compare_and_set(StateChange("late", 0, "must not publish"))
            except Exception as exc:
                rejected.append(exc)
            return "stopped later"

        runtime = app.session.extension_runtime
        runtime._extensions[0].api.register_task_handler("stubborn", stubborn)
        await app.start()
        task_id = await context(app).services.tasks.submit(TaskSpec("stubborn", {}))
        await started.wait()
        old_job = app.session.host_services.tasks._jobs[task_id]
        try:
            result = await runtime.aclose()
            assert not result.drained
            assert result.contained_managed_tasks == 1
            status = await app.session.host_services.tasks.status(old_job.token, task_id)
            assert status.status == "cancelling"
        finally:
            release.set()
            await asyncio.gather(*app.session.host_services.tasks._running.values())
        assert rejected
        assert (await runtime.aclose()).drained


async def test_failed_setup_cannot_resurrect_handlers_through_a_captured_api(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        captured = []

        async def handler(payload, task_context):
            return payload

        def failed(api):
            captured.append(api)
            api.register_task_handler("leaked", handler)
            raise ValueError("setup rejected")

        runtime = app.session.extension_runtime
        # Exercise the same isolated registration path as filesystem setup.
        original = runtime._extensions[0]
        runtime._setup_extension(
            LoadedExtension(
                name="failed",
                path=extension,
                setup=failed,
                source_id="failed-source",
                source=original.source,
            )
        )
        assert "failed-source" not in runtime._task_handlers
        with pytest.raises(ExtensionError, match="setup failed"):
            captured[0].register_task_handler("resurrected", handler)
        await app.start()
        assert runtime.active
        with pytest.raises(TaskRejected, match="not registered"):
            await context(app).services.tasks.submit(TaskSpec("leaked", {}))


async def test_task_failure_and_shutdown_leave_terminal_statuses(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    app = await CodingApplication.open(opts, provider=ReplyProvider())

    async def failure(payload, task_context):
        raise OSError("expected failure")

    async def pending(payload, task_context):
        await asyncio.Event().wait()

    api = app.session.extension_runtime._extensions[0].api
    api.register_task_handler("failure", failure)
    api.register_task_handler("pending", pending)
    await app.start()
    tasks = context(app).services.tasks
    failed_id = await tasks.submit(TaskSpec("failure", {}))
    result = await completed(tasks, failed_id)
    assert result.status == "failed"
    assert "expected failure" in result.error
    pending_id = await tasks.submit(TaskSpec("pending", {}))
    database = app.manager.paths.database_path
    await app.aclose()
    import sqlite3

    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM extension_tasks WHERE task_id=?",
            (pending_id,),
        ).fetchone()[0]
        assert status == "cancelled"
