"""Provider configuration for Run Agent coding sessions, read from the environment.

Two wire protocols exist: OpenAI-compatible (chat completions or responses) and
Anthropic Messages. Everything a session needs to call either one - base URL, API
key, default model, thinking level, timeouts - comes from environment variables,
the way Pi's ``env-api-keys`` resolves ambient keys. There is no durable provider
file, no catalog and no login flow: a ``.env`` in the project is the configuration.

Variables:

``PROVIDER``
    Which provider a session starts with (``openai`` or ``anthropic``). Defaults to
    ``anthropic`` when only ``ANTHROPIC_API_KEY`` is set, otherwise ``openai``.
``MODEL``
    Default model for whichever provider is selected.
``REASONING_EFFORT``
    Default thinking level: ``off``, ``minimal``, ``low``, ``medium``, ``high``,
    ``xhigh`` or ``max``.
``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_API``
    OpenAI-compatible key, endpoint and API flavour (``openai-completions`` or
    ``openai-responses``).
``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_THINKING_MODE``
    Anthropic key, endpoint and thinking mode (``budget`` or ``adaptive``).
``<PREFIX>_TIMEOUT_SECONDS`` / ``<PREFIX>_MAX_RETRIES`` / ``<PREFIX>_MAX_RETRY_DELAY_SECONDS``
    Transport limits per provider prefix.
``MODEL_CONTEXT_WINDOW`` / ``MODEL_MAX_TOKENS`` / ``MODEL_SUPPORTS_IMAGES``
    Optional model metadata the endpoint does not report.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ
from typing import Any, Literal, Protocol

from run_agent_ai.env import (
    CACHE_RETENTION_NONE,
    CACHE_RETENTION_SHORT,
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    AnthropicConfig,
    CacheRetention,
    OpenAICompatibleConfig,
)
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    ThinkingLevel,
    ThinkingParameter,
    anthropic_thinking_budget_for_level,
    normalize_thinking_level,
    reasoning_effort_for_level,
)

ProviderApi = Literal["openai-completions", "openai-responses", "anthropic-messages"]
ModelInput = Literal["text", "image"]
AnthropicThinkingMode = Literal["budget", "adaptive"]

DEFAULT_PROVIDER_NAME = "openai"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


class ProviderConfigError(ValueError):
    """Raised when Run Agent provider configuration is invalid."""


class CredentialReader(Protocol):
    """Credential lookup by name; the environment is the only backing store."""

    def get(self, name: str) -> str | None: ...


class EnvironmentCredentials:
    """Credentials read from a process environment, by variable name."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = environment if environment is not None else environ

    def get(self, name: str) -> str | None:
        value = self._environment.get(name)
        return value or None


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProviderConfig:
    """One OpenAI-compatible endpoint and the defaults a session starts from."""

    name: str = DEFAULT_PROVIDER_NAME
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    api: ProviderApi = "openai-completions"
    api_key_env: str = "OPENAI_API_KEY"
    models: tuple[str, ...] = ()
    """Models the provider is known to serve; empty means any model id is accepted."""
    default_model: str = DEFAULT_MODEL
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] = THINKING_LEVELS
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter = "reasoning_effort"
    thinking_format: str | None = None
    """Vendor reasoning dialect; ``None`` detects it from the base URL."""
    context_window: int | None = None
    max_tokens: int | None = None
    supports_images: bool = False

    def __post_init__(self) -> None:
        _validate_provider_numbers(
            self.timeout_seconds, self.max_retries, self.max_retry_delay_seconds
        )
        _validate_thinking(self.thinking_levels, self.thinking_default)


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    """One Anthropic Messages endpoint and the defaults a session starts from."""

    name: str = "anthropic"
    base_url: str = DEFAULT_ANTHROPIC_BASE_URL
    api: ProviderApi = "anthropic-messages"
    api_key_env: str = "ANTHROPIC_API_KEY"
    models: tuple[str, ...] = ()
    default_model: str = DEFAULT_ANTHROPIC_MODEL
    headers: dict[str, str] = field(default_factory=dict)
    compat: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
    thinking_levels: tuple[ThinkingLevel, ...] = THINKING_LEVELS
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter = "anthropic.thinking"
    thinking_mode: AnthropicThinkingMode = "budget"
    context_window: int | None = None
    max_tokens: int | None = None
    supports_images: bool = True

    def __post_init__(self) -> None:
        _validate_provider_numbers(
            self.timeout_seconds, self.max_retries, self.max_retry_delay_seconds
        )
        _validate_thinking(self.thinking_levels, self.thinking_default)


ProviderConfig = OpenAICompatibleProviderConfig | AnthropicProviderConfig


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """The providers a host can start sessions with, and which one is the default."""

    default_provider: str = DEFAULT_PROVIDER_NAME
    providers: tuple[ProviderConfig, ...] = field(
        default_factory=lambda: (OpenAICompatibleProviderConfig(), AnthropicProviderConfig())
    )

    def get_provider(self, name: str | None = None) -> ProviderConfig:
        """Return a configured provider by name."""
        target = name or self.default_provider
        for provider in self.providers:
            if provider.name == target:
                return provider
        available = ", ".join(provider.name for provider in self.providers) or "none"
        raise ProviderConfigError(f"Unknown provider: {target}. Available providers: {available}")


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    """Resolved provider/model selection for a Run Agent run."""

    provider: ProviderConfig
    model: str


def provider_settings_from_env(environment: Mapping[str, str] | None = None) -> ProviderSettings:
    """Build both providers from the environment, and pick the default between them."""
    env = environment if environment is not None else environ
    model = env.get("MODEL") or None
    thinking_default = _thinking_level_from_env(env, "REASONING_EFFORT")
    context_window = _positive_int_from_env(env, "MODEL_CONTEXT_WINDOW")
    max_tokens = _positive_int_from_env(env, "MODEL_MAX_TOKENS")
    supports_images = _flag_from_env(env, "MODEL_SUPPORTS_IMAGES")

    openai_api = env.get("OPENAI_API") or "openai-completions"
    if openai_api not in {"openai-completions", "openai-responses"}:
        raise ProviderConfigError("OPENAI_API must be openai-completions or openai-responses")
    openai = OpenAICompatibleProviderConfig(
        base_url=(env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_COMPATIBLE_BASE_URL).rstrip("/"),
        api=openai_api,  # type: ignore[arg-type]
        default_model=model or DEFAULT_MODEL,
        timeout_seconds=_positive_float_from_env(
            env, "OPENAI_TIMEOUT_SECONDS", DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
        ),
        max_retries=_non_negative_int_from_env(
            env, "OPENAI_MAX_RETRIES", DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
        ),
        max_retry_delay_seconds=_non_negative_float_from_env(
            env, "OPENAI_MAX_RETRY_DELAY_SECONDS", DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS
        ),
        thinking_default=thinking_default,
        thinking_format=env.get("OPENAI_THINKING_FORMAT") or None,
        context_window=context_window,
        max_tokens=max_tokens,
        supports_images=supports_images if supports_images is not None else False,
    )
    thinking_mode = env.get("ANTHROPIC_THINKING_MODE") or "budget"
    if thinking_mode not in {"budget", "adaptive"}:
        raise ProviderConfigError("ANTHROPIC_THINKING_MODE must be budget or adaptive")
    anthropic = AnthropicProviderConfig(
        base_url=_normalize_anthropic_base_url(
            env.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL
        ),
        default_model=model or DEFAULT_ANTHROPIC_MODEL,
        timeout_seconds=_positive_float_from_env(
            env, "ANTHROPIC_TIMEOUT_SECONDS", DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS
        ),
        max_retries=_non_negative_int_from_env(
            env, "ANTHROPIC_MAX_RETRIES", DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES
        ),
        max_retry_delay_seconds=_non_negative_float_from_env(
            env,
            "ANTHROPIC_MAX_RETRY_DELAY_SECONDS",
            DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
        ),
        thinking_default=thinking_default,
        thinking_mode=thinking_mode,  # type: ignore[arg-type]
        context_window=context_window,
        max_tokens=max_tokens,
        supports_images=supports_images if supports_images is not None else True,
    )
    default_provider = env.get("PROVIDER") or (
        "anthropic"
        if env.get("ANTHROPIC_API_KEY") and not env.get("OPENAI_API_KEY")
        else DEFAULT_PROVIDER_NAME
    )
    if default_provider not in {openai.name, anthropic.name}:
        raise ProviderConfigError(f"PROVIDER must be {openai.name} or {anthropic.name}")
    return ProviderSettings(default_provider=default_provider, providers=(openai, anthropic))


def load_provider_settings(paths: RunAgentPaths | None = None) -> ProviderSettings:
    """Provider settings for a host; ``paths`` is accepted for call-site symmetry only."""
    del paths
    return provider_settings_from_env()


def resolve_provider_selection(
    settings: ProviderSettings,
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> ProviderSelection:
    """Resolve the provider and model for a run."""
    provider = settings.get_provider(provider_name)
    selected_model = model or provider.default_model
    if not selected_model:
        raise ProviderConfigError(f"Provider {provider.name} does not define a default model")
    validate_provider_model(provider, selected_model)
    return ProviderSelection(provider=provider, model=selected_model)


def validate_provider_model(provider: ProviderConfig, model: str) -> None:
    """Raise when ``provider`` declares a model list and ``model`` is not on it."""
    if not model.strip():
        raise ProviderConfigError(f"Model id for provider {provider.name} is empty")
    if not provider.models or model in provider.models:
        return
    available = ", ".join(sorted(provider.models))
    raise ProviderConfigError(
        f"Model is not configured for provider {provider.name}: {model}. "
        f"Available models: {available}"
    )


def provider_thinking_levels(
    provider: ProviderConfig, *, model: str | None = None
) -> tuple[ThinkingLevel, ...]:
    """Thinking levels a provider accepts; the endpoint decides what a model honours."""
    del model
    return provider.thinking_levels


def provider_thinking_unavailable_reason(
    provider: ProviderConfig, *, model: str | None = None
) -> str | None:
    """Explain why a provider has no configurable thinking modes, or ``None``."""
    if provider_thinking_levels(provider, model=model):
        return None
    return f"{provider.name} declares no configurable thinking levels"


def provider_default_thinking_level(
    provider: ProviderConfig, *, model: str | None = None
) -> ThinkingLevel | None:
    """The thinking level a fresh session on this provider starts with."""
    levels = provider_thinking_levels(provider, model=model)
    if not levels:
        return None
    if provider.thinking_default in levels:
        return provider.thinking_default
    if DEFAULT_THINKING_LEVEL in levels:
        return DEFAULT_THINKING_LEVEL
    return levels[0]


def resolve_startup_thinking_level(
    provider: ProviderConfig,
    model: str,
    *,
    preferred: ThinkingLevel = DEFAULT_THINKING_LEVEL,
    cli_override: ThinkingLevel | None = None,
) -> ThinkingLevel | None:
    """Pick a valid startup thinking level.

    ``cli_override`` (``--thinking``) is strict and wins; otherwise the environment's
    ``REASONING_EFFORT`` wins over ``preferred``, which wins over the first level.
    Returns ``None`` when the provider has no configurable levels.
    """
    levels = provider_thinking_levels(provider, model=model)
    if cli_override is not None:
        if not levels:
            raise ProviderConfigError(f"Thinking modes are unavailable for {provider.name}:{model}")
        if cli_override not in levels:
            allowed = ", ".join(levels)
            raise ProviderConfigError(
                f'Thinking mode "{cli_override}" is not available for '
                f"{provider.name}:{model}. Available modes: {allowed}"
            )
        return cli_override
    if not levels:
        return None
    if provider.thinking_default in levels:
        return provider.thinking_default
    if preferred in levels:
        return preferred
    return provider_default_thinking_level(provider, model=model) or levels[0]


def provider_model_supports_images(provider: ProviderConfig, model: str | None = None) -> bool:
    del model
    return provider.supports_images


def provider_has_usable_credentials(
    provider: ProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
) -> bool:
    """Whether the provider's API key variable is set."""
    reader = credential_reader or EnvironmentCredentials()
    return bool(reader.get(provider.api_key_env))


def openai_compatible_config_from_provider(
    provider: OpenAICompatibleProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> OpenAICompatibleConfig:
    """Build the runtime config for one OpenAI-compatible request stream."""
    selected_model = model or provider.default_model
    return OpenAICompatibleConfig(
        api_key=_api_key(provider, credential_reader),
        provider_name=provider.name,
        api=provider.api,
        base_url=provider.base_url.rstrip("/"),
        headers=dict(provider.headers) or None,
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
        max_retry_delay_seconds=provider.max_retry_delay_seconds,
        max_tokens=provider.max_tokens,
        supports_images=provider.supports_images,
        reasoning_effort=_reasoning_effort(provider, selected_model, thinking_level),
        reasoning_effort_parameter=provider.thinking_parameter,
        thinking_format=provider.thinking_format or _detect_thinking_format(provider.base_url),
        compat=dict(provider.compat),
        include_reasoning_effort_none=(
            thinking_level is not None and normalize_thinking_level(thinking_level) == "off"
        ),
    )


def anthropic_config_from_provider(
    provider: AnthropicProviderConfig,
    *,
    credential_reader: CredentialReader | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
) -> AnthropicConfig:
    """Build the runtime config for one Anthropic Messages request stream."""
    selected_model = model or provider.default_model
    level = _checked_level(provider, selected_model, thinking_level)
    cache_retention, cache_control_on_tools = anthropic_cache_settings(provider, selected_model)
    if level is None or level == "off":
        thinking_mode = (
            "disabled" if level == "off" and provider.thinking_mode == "adaptive" else "budget"
        )
        budget, effort = None, None
    elif provider.thinking_mode == "adaptive":
        thinking_mode, budget, effort = "adaptive", None, level
    else:
        thinking_mode, budget, effort = "budget", anthropic_thinking_budget_for_level(level), None
    return AnthropicConfig(
        api_key=_api_key(provider, credential_reader),
        provider_name=provider.name,
        base_url=_normalize_anthropic_base_url(provider.base_url),
        cache_retention=cache_retention,
        cache_control_on_tools=cache_control_on_tools,
        headers=dict(provider.headers) or None,
        timeout_seconds=provider.timeout_seconds,
        max_retries=provider.max_retries,
        max_retry_delay_seconds=provider.max_retry_delay_seconds,
        max_tokens=provider.max_tokens,
        supports_images=provider.supports_images,
        thinking_budget_tokens=budget,
        thinking_effort=effort,
        thinking_mode=thinking_mode,
    )


def anthropic_cache_settings(
    provider: ProviderConfig, model: str | None = None
) -> tuple[CacheRetention, bool]:
    """Prompt-cache settings for one Anthropic-protocol request, from compat flags."""
    del model
    compat = provider.compat
    if compat.get("supportsCacheControl") is False:
        return CACHE_RETENTION_NONE, False
    return CACHE_RETENTION_SHORT, compat.get("supportsCacheControlOnTools") is not False


def _api_key(provider: ProviderConfig, credential_reader: CredentialReader | None) -> str:
    key = (credential_reader or EnvironmentCredentials()).get(provider.api_key_env)
    if not key:
        raise ProviderConfigError(
            f"Provider {provider.name} needs an API key: set {provider.api_key_env}"
        )
    return key


def _checked_level(
    provider: ProviderConfig, model: str, thinking_level: ThinkingLevel | None
) -> ThinkingLevel | None:
    if thinking_level is None:
        return None
    levels = provider_thinking_levels(provider, model=model)
    if not levels:
        return None
    normalized = normalize_thinking_level(thinking_level)
    if normalized not in levels:
        available = ", ".join(levels)
        raise ProviderConfigError(
            f"Thinking mode {normalized} is not available for "
            f"{provider.name}:{model}. Available modes: {available}"
        )
    return normalized


def _reasoning_effort(
    provider: OpenAICompatibleProviderConfig, model: str, thinking_level: ThinkingLevel | None
) -> str | None:
    level = _checked_level(provider, model, thinking_level)
    return reasoning_effort_for_level(level) if level is not None else None


def _detect_thinking_format(base_url: str) -> str:
    """Pick the vendor reasoning dialect from the endpoint host, as Pi's compat does."""
    if "deepseek.com" in base_url:
        return "deepseek"
    if "api.z.ai" in base_url:
        return "zai"
    if "api.together.ai" in base_url:
        return "together"
    if "openrouter.ai" in base_url:
        return "openrouter"
    return "openai"


def _normalize_anthropic_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def _validate_provider_numbers(timeout: float, retries: int, delay: float) -> None:
    if timeout <= 0:
        raise ProviderConfigError("timeout_seconds must be greater than 0")
    if retries < 0:
        raise ProviderConfigError("max_retries must be 0 or greater")
    if delay < 0:
        raise ProviderConfigError("max_retry_delay_seconds must be 0 or greater")


def _validate_thinking(levels: tuple[ThinkingLevel, ...], default: ThinkingLevel | None) -> None:
    if len(set(levels)) != len(levels):
        raise ProviderConfigError("thinking_levels must be unique")
    if default is not None and levels and default not in levels:
        raise ProviderConfigError(f"thinking_default {default} is not in thinking_levels")


def _thinking_level_from_env(env: Mapping[str, str], name: str) -> ThinkingLevel | None:
    raw = env.get(name)
    if not raw:
        return None
    try:
        return normalize_thinking_level(raw)
    except ValueError as exc:
        raise ProviderConfigError(f"{name}: {exc}") from exc


def _flag_from_env(env: Mapping[str, str], name: str) -> bool | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_from_env(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProviderConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ProviderConfigError(f"{name} must be greater than 0")
    return value


def _non_negative_int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProviderConfigError(f"{name} must be an integer") from exc
    if value < 0:
        raise ProviderConfigError(f"{name} must be 0 or greater")
    return value


def _positive_float_from_env(env: Mapping[str, str], name: str, default: float) -> float:
    value = _non_negative_float_from_env(env, name, default)
    if value <= 0:
        raise ProviderConfigError(f"{name} must be greater than 0")
    return value


def _non_negative_float_from_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ProviderConfigError(f"{name} must be a number") from exc
    if value < 0:
        raise ProviderConfigError(f"{name} must be 0 or greater")
    return value
