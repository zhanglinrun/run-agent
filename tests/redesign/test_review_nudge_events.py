"""Tool-loop nudges survive ordinary events until the durable run completion."""

import json

import pytest
from tests.redesign.test_extension_tasks import completed
from tests.redesign.test_host_services import context
from tests.redesign.test_review_closure import (
    ReviewingProvider,
    experience_options,
    review_task_id,
)

from run_agent_coding.application import CodingApplication
from run_agent_core.messages import AssistantMessage, TextContent, ToolCall
from run_agent_core.provider_events import AssistantDoneEvent
from run_agent_extensions.experience.review_models import REVIEW_SYSTEM_PROMPT, REVIEW_TASK_PREFIX


class ReadingProvider(ReviewingProvider):
    def __init__(self, iterations):
        super().__init__(
            json.dumps(
                {
                    "memory": [],
                    "skills": [
                        {
                            "action": "create",
                            "name": "verify-settings",
                            "description": "Verify project settings before editing them.",
                            "body": "# Verify settings\n\n## Procedure\n1. Read fixture.txt.\n",
                        }
                    ],
                }
            )
        )
        self.iterations = iterations
        self.calls = 0

    async def stream_response(self, *, messages, system="", **kwargs):
        if system == REVIEW_SYSTEM_PROMPT:
            async for event in super().stream_response(messages=messages, system=system, **kwargs):
                yield event
            return
        if not kwargs.get("tools"):
            yield AssistantDoneEvent(
                reason="stop",
                message=AssistantMessage(
                    content="Settings inspection", model="test", provider="test"
                ),
            )
            return
        self.calls += 1
        pending = self.calls <= self.iterations
        reason = "toolUse" if pending else "stop"
        content = (
            [ToolCall(id=f"read-{self.calls}", name="read", arguments={"path": "fixture.txt"})]
            if pending
            else [TextContent(text="Inspection complete.")]
        )
        yield AssistantDoneEvent(
            reason=reason,
            message=AssistantMessage(
                content=content, model="test", provider="test", stop_reason=reason
            ),
        )


@pytest.mark.parametrize("iterations", [8, 9])
async def test_successful_single_prompt_reviews_at_default_model_round_cadence(
    tmp_path, iterations
):
    (tmp_path / "fixture.txt").write_text("settings\n", encoding="utf-8")
    provider = ReadingProvider(iterations)
    async with await CodingApplication.open(experience_options(tmp_path), provider=provider) as app:
        await app.start()
        events = [event async for event in app.prompt("Inspect the project settings.")]
        receipt = events[-1]
        assert receipt.status == "succeeded"
        # N tool rounds plus the final stop round; skill nudge counts each model round.
        assert sum(event.type == "turn_start" for event in events) == iterations + 1
        services = context(app).services
        if iterations + 1 < 10:
            assert not await services.scope("session").state.get(
                f"{REVIEW_TASK_PREFIX}{receipt.run_id}"
            )
            assert not provider.review_prompts
            return
        task_id = await review_task_id(app, receipt.run_id)
        outcome = await completed(services.tasks, task_id)
        assert outcome.status == "succeeded", outcome.error
        assert outcome.result["applied"]
        assert provider.review_prompts
    skill = tmp_path / ".run/skills/verify-settings/SKILL.md"
    assert "Read fixture.txt" in skill.read_text(encoding="utf-8")
