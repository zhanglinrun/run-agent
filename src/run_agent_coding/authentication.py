"""Credential operations shared by startup and in-session commands."""

from __future__ import annotations

import asyncio

from run_agent_coding.credentials import FileCredentialStore, credentials_path
from run_agent_coding.extensions.api import UiBridge
from run_agent_coding.oauth_registry import get_oauth_provider
from run_agent_coding.oauth_types import OAuthLoginCallbacks, OAuthPrompt, OAuthSelectPrompt
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.provider_config import load_provider_settings


async def login(
    paths: RunAgentPaths, ui: UiBridge, provider_name: str | None, method: str | None = None
) -> str:
    if not ui.has_ui:
        raise ValueError(
            "Login requires an interactive terminal. Configure credentials before --print."
        )
    settings = load_provider_settings(paths)
    if provider_name is None:
        provider_name = await ui.select(
            "Provider", [provider.name for provider in settings.providers]
        )
    if provider_name is None:
        return "Login cancelled."
    provider = settings.get_provider(provider_name)
    name = provider.credential_name
    if name is None:
        raise ValueError(
            f"This provider reads {provider.api_key_env}; configure that environment variable."
        )
    store = FileCredentialStore(credentials_path(paths))
    oauth = get_oauth_provider(provider_name)
    if method is None and oauth is not None:
        method = await ui.select("Authentication", ["subscription", "api-key"])
        if method is None:
            return "Login cancelled."
    if method == "subscription":
        if oauth is None:
            raise ValueError(f"{provider_name} has no subscription login")

        async def prompt(request: OAuthPrompt) -> str:
            answer = await ui.input(request.message, request.placeholder or "", secret=True)
            if answer is None or (not answer and not request.allow_empty):
                raise asyncio.CancelledError
            return answer

        async def select(request: OAuthSelectPrompt) -> str | None:
            labels = [option.label for option in request.options]
            answer = await ui.select(request.message, labels)
            return request.options[labels.index(answer)].id if answer is not None else None

        callbacks = OAuthLoginCallbacks(
            on_auth=lambda info: ui.notify(f"Open {info.url}\n{info.instructions or ''}"),
            on_device_code=lambda info: ui.notify(
                f"Open {info.verification_uri}; code: {info.user_code}"
            ),
            on_prompt=prompt,
            on_select=select,
            on_progress=ui.notify,
        )
        credential = await oauth.login(callbacks)
        await asyncio.to_thread(store.set_oauth, name, credential)
    else:
        value = await ui.input(f"{provider_name} API key", secret=True)
        if value is None or not value.strip():
            return "Login cancelled."
        await asyncio.to_thread(store.set_api_key, name, value)
    return f"Credentials saved for {provider_name}."


async def logout(paths: RunAgentPaths, ui: UiBridge, provider_name: str | None) -> str:
    settings = load_provider_settings(paths)
    if provider_name is None:
        if not ui.has_ui:
            raise ValueError("Specify the provider to log out")
        provider_name = await ui.select(
            "Provider", [provider.name for provider in settings.providers]
        )
    if provider_name is None:
        return "Logout cancelled."
    provider = settings.get_provider(provider_name)
    if provider.credential_name is not None:
        store = FileCredentialStore(credentials_path(paths))
        await asyncio.to_thread(store.delete, provider.credential_name)
    return (
        f"Stored credentials removed for {provider_name}. "
        "Environment credentials are managed by the shell."
    )
