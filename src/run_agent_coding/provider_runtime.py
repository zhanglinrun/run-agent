"""Runtime provider construction for Run Agent coding sessions.

Two paths build a provider: the environment-configured OpenAI-compatible or
Anthropic endpoint (``create_model_provider``) and a process-local definition an
extension registered (``create_dynamic_model_provider``). Both resolve their key
immediately before construction and neither writes anything durable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from inspect import isawaitable
from os import environ
from typing import Protocol

from run_agent_ai.anthropic import AnthropicProvider
from run_agent_ai.env import OpenAICompatibleConfig
from run_agent_ai.openai_compatible import OpenAICompatibleProvider
from run_agent_coding.extensions.providers import (
    CredentialReader,
    DynamicProvider,
    OpenAICompatibleTransport,
    ProviderAuthError,
    ProviderModel,
    ProviderRuntimeContext,
    RequiredApiKey,
    ResolvedProviderAuth,
    _MissingRequiredApiKeyError,
    json_compatible_mapping,
    resolve_provider_auth,
)
from run_agent_coding.provider_config import (
    AnthropicProviderConfig,
    EnvironmentCredentials,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    anthropic_config_from_provider,
    openai_compatible_config_from_provider,
    validate_provider_model,
)
from run_agent_coding.thinking import ThinkingLevel
from run_agent_core.provider import ModelProvider


class ClosableModelProvider(ModelProvider, Protocol):
    """Runtime provider object Run Agent owns and can close."""

    async def aclose(self) -> None:
        """Close any provider-owned resources."""
        ...


def create_model_provider(
    provider: ProviderConfig,
    *,
    credential_store: CredentialReader | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> ClosableModelProvider:
    """Create a runtime provider for an environment-configured endpoint."""
    if model is not None:
        validate_provider_model(provider, model)
    credentials = credential_store or EnvironmentCredentials()
    if isinstance(provider, AnthropicProviderConfig):
        return AnthropicProvider(
            anthropic_config_from_provider(
                provider, credential_reader=credentials, model=model, thinking_level=thinking_level
            )
        )
    if isinstance(provider, OpenAICompatibleProviderConfig):
        return OpenAICompatibleProvider(
            openai_compatible_config_from_provider(
                provider, credential_reader=credentials, model=model, thinking_level=thinking_level
            )
        )
    raise ProviderConfigError(f"Unsupported provider config: {provider.name}")


async def create_dynamic_model_provider(
    provider: DynamicProvider,
    *,
    model: str,
    credential_store: CredentialReader | None = None,
    environment: Mapping[str, str] | None = None,
) -> ClosableModelProvider:
    """Create a candidate runtime from a process-local provider definition.

    Authentication is resolved only here, immediately before construction.
    This path never converts the dynamic definition to durable settings.
    """
    selected_model = _dynamic_model(provider, model)
    auth = await _resolve_dynamic_runtime_auth(
        provider,
        credentials=(
            credential_store if credential_store is not None else EnvironmentCredentials()
        ),
        environment=environment if environment is not None else environ,
    )
    context = ProviderRuntimeContext(provider_id=provider.id, auth=auth)
    if provider.runtime_factory is not None:
        candidate = provider.runtime_factory(context, selected_model)
        runtime = await candidate if isawaitable(candidate) else candidate
        try:
            stream_response = getattr(runtime, "stream_response", None)
        except BaseException:  # extension object validation boundary
            stream_response = None
        try:
            close = getattr(runtime, "aclose", None)
        except BaseException:  # extension object validation boundary
            close = None
        if not callable(stream_response) or not callable(close):
            error = ProviderConfigError(
                f"Runtime factory for {provider.id} returned an unsupported provider"
            )
            if callable(close):
                try:
                    close_result = close()
                    if isawaitable(close_result):
                        await close_result
                except BaseException:  # preserve the validation error
                    pass
            raise error
        return runtime

    transport = provider.transport
    assert isinstance(transport, OpenAICompatibleTransport)
    selected_api = selected_model.api or transport.api
    if selected_api not in {"openai-completions", "openai-responses"}:
        raise ProviderConfigError(
            f"OpenAI-compatible dynamic provider {provider.id} cannot use api {selected_api}"
        )
    headers = _merge_dynamic_headers(
        transport.headers,
        selected_model.headers,
        auth.headers,
    )
    has_authorization = any(key.casefold() == "authorization" for key in headers)
    if auth.api_key is not None and auth.omit_authorization_header and not has_authorization:
        raise ProviderConfigError(
            f"OpenAI-compatible dynamic provider {provider.id} resolved an API key "
            "while requesting Authorization omission"
        )
    config = OpenAICompatibleConfig(
        api_key=auth.api_key or "",
        base_url=selected_model.base_url or transport.base_url,
        headers=headers,
        timeout_seconds=transport.timeout_seconds,
        max_retries=transport.max_retries,
        max_retry_delay_seconds=transport.max_retry_delay_seconds,
        api=selected_api,
        max_tokens=selected_model.max_tokens,
        supports_images=(
            selected_model.input_modalities is not None
            and "image" in selected_model.input_modalities
        ),
        compat=json_compatible_mapping(selected_model.compat),
        provider_name=provider.id,
        omit_authorization_header=auth.omit_authorization_header,
        # Dynamic providers explicitly own their API choice. A local model id
        # resembling gpt-* or *codex* must not reroute to /responses.
        infer_api_from_model=False,
    )
    return OpenAICompatibleProvider(config, client=transport.client)


async def _resolve_dynamic_runtime_auth(
    provider: DynamicProvider,
    *,
    credentials: CredentialReader,
    environment: Mapping[str, str],
) -> ResolvedProviderAuth:
    """Resolve extension auth behind a categorical secret-safe boundary."""
    try:
        return await resolve_provider_auth(
            provider.auth,
            credentials=credentials,
            environment=environment,
        )
    except asyncio.CancelledError:
        # Keep cancellation semantics without retaining an extension-authored
        # cancellation message that could contain credential material.
        raise asyncio.CancelledError from None
    except _MissingRequiredApiKeyError:
        # Preserve only Run Agent's exact strategy and host-authored missing-key error.
        if type(provider.auth) is RequiredApiKey:
            raise
        raise ProviderAuthError("Dynamic provider authentication resolution failed") from None
    except ProviderAuthError:
        # Custom strategies can raise ProviderAuthError too, so their arbitrary
        # text crosses the same categorical boundary as any extension exception.
        raise ProviderAuthError("Dynamic provider authentication resolution failed") from None
    except BaseException:  # extension authentication boundary
        raise ProviderAuthError("Dynamic provider authentication resolution failed") from None


def _dynamic_model(provider: DynamicProvider, model: str) -> ProviderModel:
    for candidate in provider.models:
        if candidate.id == model:
            return candidate
    raise ProviderConfigError(f"Model is not configured for provider {provider.id}: {model}")


def _merge_dynamic_headers(*values: Mapping[str, str]) -> dict[str, str]:
    """Merge transport/model/auth headers case-insensitively, latest value winning."""
    merged: dict[str, str] = {}
    names: dict[str, str] = {}
    for value in values:
        for key, item in value.items():
            normalized = key.casefold()
            previous = names.get(normalized)
            if previous is not None:
                merged.pop(previous)
            names[normalized] = key
            merged[key] = item
    return merged
