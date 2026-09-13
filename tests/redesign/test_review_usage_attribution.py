"""The review's usage is attributed to the parent run in production.

The mechanism was already tested in isolation, but a tested mechanism that nothing
constructs is not a feature. This drives the real extension through the real command
path and requires the task result to carry the usage, bound to the run that caused the
review.

Failed calls are attempted requests too. Without a usage receipt their token spend
is unknown, and must remain attributed without presenting missing usage as free.
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


async def test_the_review_reports_usage_bound_to_the_parent_run(tmp_path):
    opts = replace(options(tmp_path), extension_paths=(EXPERIENCE,), extensions_enabled=True)
    async with await CodingApplication.open(opts, provider=FailingProvider()) as app:
        await app.start()
        run_id = [event async for event in app.prompt("please fail")][-1].run_id
        services = context(app).services

        task_id = await services.tasks.submit(
            TaskSpec(handler=REVIEW_HANDLER, payload={"run_id": run_id})
        )
        task = await completed(services.tasks, task_id)

        assert task.status == "succeeded", task.error
        assert task.result["consumed"] == run_id
        usage = task.result["usage"]
        assert usage["parent_run_id"] == run_id
        assert usage["requests"] == 1
        assert usage["input_tokens"] is None
        assert usage["unreported_requests"] == 1
        assert task.result["status"] == "failed"
