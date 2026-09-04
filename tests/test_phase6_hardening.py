"""Phase 6 lifecycle, race, ownership, and persistence regressions."""

from __future__ import annotations

import asyncio

import pytest

from run_agent_coding.extensions import (
    DynamicProvider,
    ExtensionRuntime,
    NoAuth,
    OpenAICompatibleTransport,
    ProviderModel,
    ProviderModelSnapshot,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _provider(provider_id: str = "second", *, refresh_models=None) -> DynamicProvider:
    return DynamicProvider(
        id=provider_id,
        display_name=provider_id,
        models=(ProviderModel("model"),),
        default_model="model",
        transport=OpenAICompatibleTransport(
            base_url="http://second.test/v1",
            auth=NoAuth(),
        ),
        refresh_models=refresh_models,
    )


async def test_refresh_stress_keeps_latest_snapshot_and_closes_generation() -> None:
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(context) -> ProviderModelSnapshot:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        model = f"model-{calls}"
        return ProviderModelSnapshot((ProviderModel(model),), model)

    runtime = ExtensionRuntime()
    registry = runtime.provider_registry
    registry.register("test:second", _provider(refresh_models=refresh))
    waiters = [asyncio.create_task(registry.refresh("second")) for _ in range(20)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*waiters)

    assert calls == 1
    assert {result.status for result in results} == {"published"}
    assert registry.effective("second").definition.models[0].id == "model-1"  # type: ignore[union-attr]
    await runtime.aclose()
    assert registry.effective("second") is None


async def test_refresh_from_retired_generation_cannot_publish() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def refresh(context) -> ProviderModelSnapshot:
        entered.set()
        await release.wait()
        return ProviderModelSnapshot((ProviderModel("stale"),), "stale")

    runtime = ExtensionRuntime()
    registry = runtime.provider_registry
    registry.register("test:second", _provider())
    # Replace the callback while keeping the test provider's public shape.
    current = registry.effective("second").definition  # type: ignore[union-attr]
    registry.register(
        "test:second",
        DynamicProvider(
            id=current.id,
            display_name=current.display_name,
            models=current.models,
            default_model=current.default_model,
            transport=current.transport,
            refresh_models=refresh,
        ),
    )
    pending = asyncio.create_task(registry.refresh("second"))
    await entered.wait()
    runtime.reset_for_reload()
    release.set()
    result = await pending

    assert result.status == "cancelled"
    assert registry.effective("second") is None
    await runtime.aclose()
