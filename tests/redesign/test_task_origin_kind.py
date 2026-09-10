"""RED: a managed task carries a host-assigned origin kind (P5-2).

The host, not the extension and not the payload, decides whether a task is
ordinary user work or an auxiliary task such as a review. Auxiliary work must be
excludable from triggering further reviews, and the classification has to survive
a round trip through storage so it can be queried later - which is also what A05
means by the convergence state being inspectable.
"""

from dataclasses import replace

from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import TaskSpec

WORKER_SETUP = """
def setup(api):
    async def work(payload, task):
        return {"done": True}

    api.register_task_handler("origin-probe", work)
"""


def test_the_default_origin_kind_is_user_work() -> None:
    assert TaskSpec(handler="anything", payload=None).origin_kind == "user"


async def test_the_origin_kind_survives_storage_and_is_queryable(tmp_path):
    extension = tmp_path / "worker.py"
    extension.write_text(WORKER_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        services = context(app).services
        auxiliary = await services.tasks.submit(
            TaskSpec(handler="origin-probe", payload=None, origin_kind="review")
        )
        ordinary = await services.tasks.submit(
            TaskSpec(handler="origin-probe", payload=None, origin_kind="user")
        )
        assert (await completed(services.tasks, auxiliary)).origin_kind == "review"
        assert (await completed(services.tasks, ordinary)).origin_kind == "user"
