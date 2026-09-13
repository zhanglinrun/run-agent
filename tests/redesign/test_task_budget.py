"""A managed task declares a budget, and the host records it.

After an extension is closed, the task's cancellation, budget and resource
convergence are all inspectable: a task declares its ceilings, the declaration
survives storage, and it reads back with the task's terminal status.
"""

import json
from dataclasses import replace

from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import TaskBudget, TaskSpec

WORKER_SETUP = """
def setup(api):
    async def work(payload, task):
        return {"done": True}

    api.register_task_handler("budget-probe", work)
"""


def test_the_default_budget_is_bounded_rather_than_unlimited() -> None:
    budget = TaskBudget()
    assert budget.max_requests > 0
    assert budget.unlimited_tokens is False


def test_a_zero_token_ceiling_means_uncapped_not_no_capacity() -> None:
    assert TaskBudget(max_tokens=0).unlimited_tokens is True
    assert TaskBudget(max_tokens=1_000).unlimited_tokens is False


def test_the_budget_defaults_to_the_contract_default() -> None:
    assert TaskSpec(handler="anything", payload=None).budget == TaskBudget()


async def test_the_declared_budget_survives_storage_and_reads_back(tmp_path):
    extension = tmp_path / "worker.py"
    extension.write_text(WORKER_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        services = context(app).services
        declared = TaskBudget(max_requests=2, max_tokens=5_000)
        task_id = await services.tasks.submit(
            TaskSpec(handler="budget-probe", payload=None, budget=declared)
        )
        task = await completed(services.tasks, task_id)
        assert task.status == "succeeded"
        # The budget must still be inspectable once the task is terminal.
        assert task.budget == declared


async def test_an_unbounded_budget_round_trips_as_uncapped(tmp_path):
    extension = tmp_path / "worker.py"
    extension.write_text(WORKER_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        services = context(app).services
        task_id = await services.tasks.submit(
            TaskSpec(handler="budget-probe", payload=None, budget=TaskBudget(max_tokens=0))
        )
        task = await completed(services.tasks, task_id)
        assert task.budget.unlimited_tokens is True


async def test_a_closed_extension_still_reports_status_budget_and_convergence(tmp_path):
    """Cancellation, budget and convergence all stay inspectable after close."""
    extension = tmp_path / "worker.py"
    extension.write_text(WORKER_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    app = await CodingApplication.open(opts, provider=ReplyProvider())
    await app.start()
    services = context(app).services
    declared = TaskBudget(max_requests=3, max_tokens=4_000)
    task_id = await services.tasks.submit(
        TaskSpec(handler="budget-probe", payload=None, budget=declared)
    )
    await completed(services.tasks, task_id)

    result = await app.session.extension_runtime.aclose()

    assert result.drained is True
    assert result.contained_managed_tasks == 0
    assert result.cleanup_errors == ()
    row = await app.session.storage.repository.database.run(
        lambda connection: connection.execute(
            "SELECT status, budget_json FROM extension_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    )
    assert row[0] == "succeeded"
    assert TaskBudget.from_json(json.loads(row[1])) == declared
    await app.aclose()
