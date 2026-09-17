"""Experience consumes the durable completion receipt.

The trigger policy is unit-tested in test_review_trigger.py. These tests cover
the wiring: a real session runs, the extension sees the completion, and only an
admitted decision leaves a durable review request behind.
"""

from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_core.messages import AssistantMessage, TextContent
from run_agent_core.provider_events import AssistantErrorEvent

REPO = Path(__file__).resolve().parents[2]
EXPERIENCE = REPO / "src" / "run_agent_extensions" / "experience"


class FailingProvider:
    """A provider whose run fails, which is what the trigger treats as worth reviewing."""

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


def experience_options(tmp_path):
    return replace(options(tmp_path), extension_paths=(EXPERIENCE,), extensions_enabled=True)


async def review_request(app, run_id: str):
    state = context(app).services.scope("session").state
    return await state.get(f"review-request:{run_id}")


async def test_a_failed_completion_leaves_one_durable_review_request(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPERIENCE_REVIEW_ON_SIGNALS", "true")
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        events = [event async for event in app.prompt("please fail")]
        run_id = events[-1].run_id
        assert events[-1].status == "failed"

        recorded = await review_request(app, run_id)
        assert recorded is not None, "a failed run must be queued for review"
        assert recorded.value["run_id"] == run_id
        assert recorded.value["key"] == f"{run_id}:1"


async def test_experience_maintenance_unregisters_when_session_closes(tmp_path):
    maintenance = None
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        maintenance = context(app).services.maintenance
        assert "experience-curator" in maintenance.names
    assert maintenance is not None
    assert "experience-curator" not in maintenance.names

    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        events = [event async for event in app.prompt("hello")]
        run_id = events[-1].run_id
        assert events[-1].status == "succeeded"

        # One turn with no correction and no failure is exactly the chitchat the
        # policy exists to skip, so the wiring must not record anything.
        assert await review_request(app, run_id) is None
