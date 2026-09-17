"""A review's own writes are marked agent-created.

The provenance variable has to be bound by the review fork itself, not only
available by hand. Anything the worker consumes is written under the review
origin, which is exactly the set of assets automatic maintenance may later touch.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from tests.redesign.test_coding_application import options
from tests.redesign.test_experience_review_wiring import FailingProvider
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context
from tests.redesign.test_review_closure import review_task_id

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import TaskSpec
from run_agent_coding.host.learning import is_agent_created

REPO = Path(__file__).resolve().parents[2]
EXPERIENCE = REPO / "src" / "run_agent_extensions" / "experience"


@pytest.fixture(autouse=True)
def _admit_failed_runs(monkeypatch):
    monkeypatch.setenv("EXPERIENCE_REVIEW_ON_SIGNALS", "true")


def experience_options(tmp_path):
    return replace(options(tmp_path), extension_paths=(EXPERIENCE,), extensions_enabled=True)


async def test_the_review_worker_runs_its_work_under_the_review_origin(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        run_id = [event async for event in app.prompt("please fail")][-1].run_id
        services = context(app).services

        task_id = await review_task_id(app, run_id)
        task = await completed(services.tasks, task_id)

        assert task.status == "succeeded", task.error
        assert task.result["consumed"] == run_id
        # Outside the fork the origin is the foreground learner again.
        assert is_agent_created() is False


async def test_concurrent_reviews_for_one_session_are_refused(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        run_id = [event async for event in app.prompt("please fail")][-1].run_id
        services = context(app).services
        spec = TaskSpec(handler="experience-review", payload={"run_id": run_id})
        automatic = await review_task_id(app, run_id)

        first, second = await asyncio.gather(
            services.tasks.submit(spec), services.tasks.submit(spec)
        )
        results = [
            await completed(services.tasks, task_id) for task_id in (automatic, first, second)
        ]

        assert {result.status for result in results} == {"succeeded"}
        consumed = [result.result["consumed"] for result in results]
        # Exactly one task consumes the request, including the automatically submitted task.
        assert consumed.count(run_id) == 1, consumed
