"""Review generation uses an isolated adapter, never foreground configuration."""

import asyncio
from types import SimpleNamespace

import pytest
from tests.redesign.test_host_services import context
from tests.redesign.test_inference_service_contract import UsageProvider, probe_options
from tests.redesign.test_inference_service_contract import probe as probe
from tests.redesign.test_review_runtime_accounting import loop

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.inference import InferenceRequest, InferenceResult
from run_agent_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from run_agent_extensions.experience.config import load_experience_config
from run_agent_extensions.experience.extension import _ConfigurableCoordinator
from run_agent_extensions.experience.worker import ReviewLedger


@pytest.mark.parametrize(
    "kwargs",
    [
        {"thinking_level": "invalid"},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
    ],
)
def test_invalid_generation_controls(kwargs):
    with pytest.raises(ValueError):
        InferenceRequest(prompt="review", **kwargs)


def test_review_environment_generation_controls():
    defaults = load_experience_config({})
    assert defaults.review_thinking == "off"
    assert defaults.review_max_output_tokens == 1600
    configured = load_experience_config(
        {
            "EXPERIENCE_REVIEW_THINKING": "low",
            "EXPERIENCE_REVIEW_MAX_OUTPUT_TOKENS": "2400",
        }
    )
    assert configured.review_thinking == "low"
    assert configured.review_max_output_tokens == 2400
    for env in (
        {"EXPERIENCE_REVIEW_THINKING": "bad"},
        {"EXPERIENCE_REVIEW_MAX_OUTPUT_TOKENS": "0"},
    ):
        with pytest.raises(ValueError):
            load_experience_config(env)


async def test_configured_review_policy_reaches_completion_request():
    coordinator = object.__new__(_ConfigurableCoordinator)
    coordinator.configure(
        load_experience_config(
            {
                "EXPERIENCE_REVIEW_THINKING": "low",
                "EXPERIENCE_REVIEW_MAX_OUTPUT_TOKENS": "2400",
            }
        )
    )

    async def complete(request):
        assert request.thinking_level == "low"
        assert request.max_output_tokens == 2400
        return InferenceResult("Nothing to save.", "test", "snapshot")

    result = await loop(
        SimpleNamespace(complete=complete),
        ReviewLedger(parent_run_id="source"),
        thinking_level=coordinator._policy.thinking_level,
        max_output_tokens=coordinator._policy.max_output_tokens,
    )
    assert result.stop_reason == "final answer"
    assert result.error is None


async def test_injected_provider_owns_policy_without_constructing_adapter(
    tmp_path, monkeypatch, probe
):
    def forbidden(*args, **kwargs):
        pytest.fail("injected provider must not be replaced")

    monkeypatch.setattr("run_agent_coding.session.create_model_provider", forbidden)
    provider = UsageProvider()
    async with await CodingApplication.open(
        probe_options(tmp_path, probe), provider=provider
    ) as app:
        await app.start()
        result = await app.session.inference_service.complete(
            InferenceRequest(
                prompt="review",
                thinking_level="off",
                max_output_tokens=1600,
            )
        )
        snapshot = await context(app).services.snapshots.read(result.snapshot_id)
        assert snapshot.payload["generation_controls"] == {
            "requested": {"thinking_level": "off", "max_output_tokens": 1600},
            "applied": None,
            "policy": "provider_owned",
        }
    assert provider.calls == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_static_adapter_controls_transform_and_cleanup(tmp_path, monkeypatch, cancel, probe):
    created = []
    transformed = []
    entered = asyncio.Event()

    class Provider(UsageProvider):
        closed = 0

        async def aclose(self):
            self.closed += 1

        async def stream_response(self, **kwargs):
            if cancel and self is created[-1][2] and len(created) > 1:
                entered.set()
                await asyncio.Event().wait()
            async for event in super().stream_response(**kwargs):
                yield event

    class Wrapper:
        def __init__(self, inner):
            self.inner = inner
            self.closed = 0

        def stream_response(self, **kwargs):
            return self.inner.stream_response(**kwargs)

        async def aclose(self):
            self.closed += 1
            await self.inner.aclose()

    def factory(config, *, thinking_level=None, **kwargs):
        runtime = Provider()
        created.append((config, thinking_level, runtime))
        return runtime

    def transform(provider, name):
        wrapper = Wrapper(provider)
        transformed.append((name, wrapper))
        return wrapper

    monkeypatch.setattr("run_agent_coding.session.create_model_provider", factory)
    config = OpenAICompatibleProviderConfig(
        name="test",
        default_model="test",
        thinking_default="max",
        max_tokens=12000,
    )
    settings = ProviderSettings(default_provider="test", providers=(config,))
    async with await CodingApplication.open(
        probe_options(tmp_path, probe),
        settings=settings,
        provider_transform=transform,
    ) as app:
        await app.start()
        original = app.session._harness.config.provider
        original_config = app.session._runtime_provider_config
        task = asyncio.create_task(
            app.session.inference_service.complete(
                InferenceRequest(
                    prompt="review",
                    thinking_level="off",
                    max_output_tokens=1600,
                )
            )
        )
        if cancel:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            snapshot = await context(app).services.snapshots.read(result.snapshot_id)
            assert snapshot.payload["generation_controls"]["applied"] == {
                "thinking_level": "off",
                "max_output_tokens": 1600,
            }
        selected, thinking, temporary = created[-1]
        assert selected.max_tokens == 1600
        assert thinking == "off"
        assert transformed[-1][0] == "test"
        assert temporary.closed == transformed[-1][1].closed == 1
        assert app.session._harness.config.provider is original
        assert app.session._runtime_provider_config is original_config
        assert app.session.thinking_level == "max"
        assert config.max_tokens == 12000
        assert config.thinking_default == "max"
