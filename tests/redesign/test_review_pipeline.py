"""The review worker consumes what the completion trigger queued.

The trigger records a durable review request when a completion is admitted. This covers
the next hop: the registered handler takes that request, does its work under the
review worker's claim, and records exactly one outcome per run.
"""

from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import options
from tests.redesign.test_experience_review_wiring import FailingProvider
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import TaskSpec

REPO = Path(__file__).resolve().parents[2]
EXPERIENCE = REPO / "src" / "run_agent_extensions" / "experience"
REVIEW_HANDLER = "experience-review"


def experience_options(tmp_path):
    return replace(options(tmp_path), extension_paths=(EXPERIENCE,), extensions_enabled=True)


async def run_review(app, run_id: str):
    services = context(app).services
    task_id = await services.tasks.submit(
        TaskSpec(handler=REVIEW_HANDLER, payload={"run_id": run_id})
    )
    return await completed(services.tasks, task_id)


async def test_the_review_worker_consumes_a_queued_request_exactly_once(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        events = [event async for event in app.prompt("please fail")]
        run_id = events[-1].run_id
        services = context(app).services
        assert await services.scope("session").state.get(f"review-request:{run_id}") is not None

        first = await run_review(app, run_id)
        assert first.status == "succeeded", first.error
        assert first.result["consumed"] == run_id
        assert first.result["key"] == f"{run_id}:1"

        # The request is consumed once; a second pass finds nothing left to do.
        second = await run_review(app, run_id)
        assert second.status == "succeeded", second.error
        assert second.result["consumed"] is None


async def test_the_review_worker_refuses_a_missing_run_id(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        task = await run_review(app, "")
        assert task.status == "failed"
        assert task.error is not None and "run_id" in task.error
