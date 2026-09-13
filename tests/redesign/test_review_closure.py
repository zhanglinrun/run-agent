"""The review loop actually closes: a finished run becomes edits on disk.

The pipeline decides when a completion deserves a review and records a durable
request. This covers the rest: an admitted completion submits its own review task,
the task reads the reviewed run's evidence, asks the host for one bounded completion,
and applies what came back directly to USER.md, MEMORY.md and the Skills the review
owns, the way hermes-agent's background review writes straight to its stores.

A completion event fires while its own session is still unwinding, so a review
submitted at that moment always finds the foreground busy. The gate waits a bounded
time instead of deferring forever, and a request that still did not get its turn is
re-submitted rather than dropped.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import options
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.host.contracts import StateValue, TaskInfo
from run_agent_core.messages import AssistantMessage, TextContent, Usage
from run_agent_core.provider_events import AssistantDoneEvent, AssistantErrorEvent
from run_agent_core.session.contracts import SessionConflict
from run_agent_extensions.experience.review import ReviewCoordinator
from run_agent_extensions.experience.review_models import (
    REVIEW_REQUEST_PREFIX,
    REVIEW_SYSTEM_PROMPT,
    REVIEW_TASK_PREFIX,
    ForegroundGate,
    ReviewDecision,
    ReviewPolicy,
)

REPO = Path(__file__).resolve().parents[2]
EXPERIENCE = REPO / "src" / "run_agent_extensions" / "experience"
REVIEW_HANDLER = "experience-review"
CONSUMED_PREFIX = "review-consumed:"

EDITS = {
    "memory": [
        {
            "target": "memory",
            "action": "add",
            "content": "A provider error needs an explicit retry step before giving up.",
        },
        {"target": "user", "action": "add", "content": "Wants failures reported plainly."},
    ],
    "skills": [
        {
            "action": "create",
            "name": "provider-recovery",
            "description": "Recover from a failed provider call",
            "body": "# Provider recovery\n\n## Procedure\n1. Retry once.\n",
        }
    ],
}


def experience_options(tmp_path):
    return replace(
        options(tmp_path),
        extension_paths=(EXPERIENCE,),
        extensions_enabled=True,
        trust_default="always",
    )


class ReviewingProvider:
    """Fails ordinary runs and answers the review's own request with a scripted payload.

    Keyed on the system instruction rather than call order, so a retry of the failed turn
    can never be mistaken for the review, nor the review for a retry.
    """

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.review_prompts: list[str] = []

    async def stream_response(self, *, messages, system="", **kwargs):
        if system == REVIEW_SYSTEM_PROMPT:
            self.review_prompts.append("\n".join(message.text for message in messages))
            yield AssistantDoneEvent(
                reason="stop",
                message=AssistantMessage(
                    content=[TextContent(text=self.payload)],
                    model="test",
                    provider="test",
                    stop_reason="stop",
                    usage=Usage(input=200, output=40, total_tokens=240),
                ),
            )
            return
        yield AssistantErrorEvent(
            reason="error",
            error=AssistantMessage(
                content=[TextContent(text="boom")],
                model="test",
                provider="test",
                stop_reason="error",
                error_message="injected model error",
            ),
        )


class AlwaysFailingProvider:
    async def stream_response(self, *, messages, **kwargs):
        yield AssistantErrorEvent(
            reason="error",
            error=AssistantMessage(
                content=[TextContent(text="boom")],
                model="test",
                provider="test",
                stop_reason="error",
                error_message="injected model error",
            ),
        )


async def failed_run(app) -> tuple[str, str | None]:
    events = [event async for event in app.prompt("please fail")]
    run_id = events[-1].run_id
    assert events[-1].status == "failed"
    return run_id, events[-1].snapshot_id


async def review_task_id(app, run_id: str) -> str:
    state = context(app).services.scope("session").state
    row = await state.get(f"{REVIEW_TASK_PREFIX}{run_id}")
    assert row is not None, "an admitted completion must submit its own review"
    return row.value["task_id"]


async def test_an_admitted_completion_submits_and_consumes_its_own_review(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=AlwaysFailingProvider()
    ) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        assert await services.scope("session").state.get(f"{REVIEW_REQUEST_PREFIX}{run_id}")

        task_id = await review_task_id(app, run_id)
        submitted = await services.tasks.status(task_id)
        assert submitted.handler == REVIEW_HANDLER
        assert submitted.origin_kind == "review", "a review must not be able to review itself"
        assert submitted.budget.max_requests == ReviewPolicy().max_iterations
        assert submitted.budget.max_tokens == ReviewPolicy().max_input_tokens

        outcome = await completed(services.tasks, task_id)

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["consumed"] == run_id


async def test_a_review_that_cannot_reach_the_model_records_one_outcome(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=AlwaysFailingProvider()
    ) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        outcome = await completed(services.tasks, await review_task_id(app, run_id))
        consumed = await services.scope("session").state.get(f"{CONSUMED_PREFIX}{run_id}")

    # The request is consumed exactly once even when the review could not think: a review
    # with no recorded outcome would be a review that silently retries forever.
    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["consumed"] == run_id
    assert outcome.result["applied"] == []
    assert outcome.result["error"]
    assert outcome.result["status"] == "failed"
    assert outcome.result["usage"]["requests"] == 1
    assert outcome.result["usage"]["input_tokens"] is None
    assert consumed is not None
    assert not (tmp_path / ".run" / "MEMORY.md").exists()


async def test_a_review_writes_its_edits_to_memory_and_skills_on_disk(tmp_path):
    provider = ReviewingProvider(json.dumps(EDITS))
    async with await CodingApplication.open(experience_options(tmp_path), provider=provider) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        outcome = await completed(services.tasks, await review_task_id(app, run_id))

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["consumed"] == run_id
    assert outcome.result["skipped"] == []
    assert len(outcome.result["applied"]) == 3

    # The spend belongs to the run that caused it, not to an anonymous pool.
    assert outcome.result["usage"]["parent_run_id"] == run_id
    assert outcome.result["usage"]["requests"] == 1
    assert outcome.result["usage"]["input_tokens"] == 200

    memory = (tmp_path / ".run" / "MEMORY.md").read_text(encoding="utf-8")
    assert "explicit retry step" in memory
    user = (tmp_path / "state" / "USER.md").read_text(encoding="utf-8")
    assert "reported plainly" in user
    skill = (tmp_path / ".run" / "skills" / "provider-recovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "created_by: review" in skill
    assert "Retry once" in skill

    assert provider.review_prompts, "the review must ask the host for a completion"
    assert "injected model error" in provider.review_prompts[0]


async def test_a_review_may_not_patch_a_skill_the_user_wrote(tmp_path):
    user_skill = tmp_path / ".run" / "skills" / "deploy"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy\n---\n\n# Deploy\n\n1. Ship it.\n",
        encoding="utf-8",
    )
    edits = {
        "memory": [],
        "skills": [
            {
                "action": "patch",
                "name": "deploy",
                "old_text": "1. Ship it.",
                "new_text": "1. Ship it carefully.",
            }
        ],
    }
    provider = ReviewingProvider(json.dumps(edits))
    async with await CodingApplication.open(experience_options(tmp_path), provider=provider) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        outcome = await completed(services.tasks, await review_task_id(app, run_id))

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["applied"] == []
    assert len(outcome.result["skipped"]) == 1
    assert "LearnerOwnedAsset" in outcome.result["skipped"][0]
    assert "Ship it carefully" not in (user_skill / "SKILL.md").read_text(encoding="utf-8")
    assert "[user-owned, do not patch]" in provider.review_prompts[0]


async def test_a_review_applies_nothing_from_an_unparseable_answer(tmp_path):
    provider = ReviewingProvider("I could not decide, sorry.")
    async with await CodingApplication.open(experience_options(tmp_path), provider=provider) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        outcome = await completed(services.tasks, await review_task_id(app, run_id))

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["consumed"] == run_id
    assert outcome.result["applied"] == []
    assert "unparseable" in outcome.result["error"]
    assert not (tmp_path / ".run" / "MEMORY.md").exists()


async def test_a_review_can_legitimately_find_nothing_to_save(tmp_path):
    provider = ReviewingProvider("Nothing to save.")
    async with await CodingApplication.open(experience_options(tmp_path), provider=provider) as app:
        await app.start()
        run_id, _ = await failed_run(app)
        services = context(app).services
        outcome = await completed(services.tasks, await review_task_id(app, run_id))

    assert outcome.result["status"] == "skipped"
    assert "error" not in outcome.result
    assert outcome.result["applied"] == []


async def test_the_gate_waits_for_the_foreground_instead_of_deferring_forever():
    state = {"busy": True}
    gate = ForegroundGate(busy=lambda: state["busy"])

    async def free_the_foreground():
        await asyncio.sleep(0.02)
        state["busy"] = False

    releaser = asyncio.create_task(free_the_foreground())
    assert await gate.wait_idle(timeout=2.0, interval=0.005) is None
    await releaser


async def test_the_gate_gives_up_after_its_bounded_wait():
    gate = ForegroundGate(busy=lambda: True)
    assert await gate.wait_idle(timeout=0.03, interval=0.005) == "foreground busy"


class _State:
    """A namespace state service with real compare-and-set, for the drain unit tests."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.versions: dict[str, int] = {}

    async def get(self, key) -> StateValue | None:
        if key not in self.values:
            return None
        return StateValue(key, self.versions[key], self.values[key])

    async def list(self, *, prefix: str = "", limit: int = 100) -> list[StateValue]:
        keys = sorted(key for key in self.values if key.startswith(prefix))
        return [StateValue(key, self.versions[key], self.values[key]) for key in keys[:limit]]

    async def compare_and_set(self, change) -> StateValue:
        current = self.versions.get(change.key, 0)
        if current != change.expected_version:
            raise SessionConflict(f"State version changed: {change.key}")
        self.versions[change.key] = change.expected_version + 1
        self.values[change.key] = change.value
        return StateValue(change.key, self.versions[change.key], change.value)

    def record(self, key: str, value: object) -> None:
        """Write a row directly, for state a test is pretending already exists."""
        self.versions[key] = 1
        self.values[key] = value


class _Tasks:
    def __init__(self) -> None:
        self.submitted: list[object] = []
        self.statuses: dict[str, str] = {}

    async def submit(self, spec) -> str:
        task_id = f"task-{len(self.submitted) + 1}"
        self.submitted.append(spec)
        self.statuses[task_id] = "queued"
        return task_id

    async def status(self, task_id: str) -> TaskInfo:
        return TaskInfo(task_id, REVIEW_HANDLER, self.statuses[task_id])


class _Services:
    def __init__(self) -> None:
        self.session_state = _State()
        self.tasks = _Tasks()

    def scope(self, scope: str):
        return type("Scoped", (), {"state": self.session_state})()


class _Context:
    session_id = "session-1"

    def __init__(self, services: _Services, *, is_running: bool = False) -> None:
        self.services = services
        self.is_running = is_running


class _Api:
    def __init__(self, services: _Services, *, is_running: bool = False) -> None:
        self.context = _Context(services, is_running=is_running)


def _no_stores():
    raise AssertionError("these tests never reach the stores")


class _AlwaysAdmits:
    def consider(self, request) -> ReviewDecision:
        return ReviewDecision(True, "admitted", f"{request.source_run_id}:test")


def settled_event(run_id: str, snapshot_id: str) -> AgentSettledEvent:
    return AgentSettledEvent(
        run_id=run_id,
        session_id="session-1",
        branch_id="main",
        status="failed",
        head_id=None,
        watermark=1,
        snapshot_id=snapshot_id,
    )


async def test_an_admitted_completion_submits_its_own_review_once():
    services = _Services()
    coordinator = ReviewCoordinator(_Api(services), _no_stores, trigger=_AlwaysAdmits())

    await coordinator.settled(settled_event("run-1", "snapshot-1"), None)
    await coordinator.settled(settled_event("run-1", "snapshot-1"), None)

    assert [spec.payload["run_id"] for spec in services.tasks.submitted] == ["run-1"]
    assert services.tasks.submitted[0].origin_kind == "review"
    assert (
        services.tasks.submitted[0].budget.max_requests == ReviewPolicy().budget.max_model_requests
    )
    recorded = services.session_state.values[f"{REVIEW_REQUEST_PREFIX}run-1"]
    assert recorded["snapshot_id"] == "snapshot-1"
    assert recorded["key"] == "run-1:test"


async def test_a_deferred_review_hands_the_request_a_later_turn_itself():
    services = _Services()
    coordinator = ReviewCoordinator(
        _Api(services, is_running=True),
        _no_stores,
        trigger=_AlwaysAdmits(),
        policy=ReviewPolicy(foreground_wait_seconds=0.0),
    )

    outcome = await coordinator.consume({"run_id": "run-1"}, None)

    assert outcome["consumed"] is None
    assert outcome["reason"] == "foreground busy"
    assert outcome["resubmitted"] is True
    assert [spec.payload["run_id"] for spec in services.tasks.submitted] == ["run-1"]

    # The replacement carries the attempt count forward, so a session that never idles
    # cannot grow the task table by one abandoned row per completed run, forever.
    for _ in range(ReviewPolicy().max_submissions + 2):
        final = await coordinator.consume({"run_id": "run-1"}, None)
    assert final["resubmitted"] is False
    assert len(services.tasks.submitted) == ReviewPolicy().max_submissions


async def test_a_consumed_request_never_hands_itself_another_turn():
    services = _Services()
    services.session_state.record(f"{CONSUMED_PREFIX}run-1", {"run_id": "run-1"})
    services.session_state.record(f"{REVIEW_REQUEST_PREFIX}run-1", {"run_id": "run-1"})
    coordinator = ReviewCoordinator(_Api(services), _no_stores, trigger=_AlwaysAdmits())

    outcome = await coordinator.consume({"run_id": "run-1"}, None)

    # The run already had its one outcome, so there is nothing left to hand a turn to.
    assert outcome["consumed"] is None
    assert services.tasks.submitted == []
