"""Resolve OpenAI vs Anthropic from env / CLI (C10)."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def _clean_env(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def is_anthropic_compatible_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    path = (urlparse(base_url).path or "").lower().rstrip("/")
    return path.endswith("/anthropic") or "/anthropic/" in path


def resolve_api_config(
    *,
    cli_api_base: str | None = None,
    cli_api_key: str | None = None,
) -> tuple[str | None, str | None, bool]:
    """Return (base_url, api_key, use_openai).

    URL path containing ``/anthropic`` wins over a variable named OPENAI_BASE_URL.
    """
    generic_api_key = (
        _clean_env(cli_api_key)
        or _clean_env(os.environ.get("APIKEY"))
        or _clean_env(os.environ.get("MINI_CLAUDE_API_KEY"))
    )
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))
    anthropic_api_key = _clean_env(os.environ.get("ANTHROPIC_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API")) or _clean_env(
        os.environ.get("MINI_CLAUDE_API_BASE")
    )
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))
    anthropic_api_base = _clean_env(os.environ.get("ANTHROPIC_BASE_URL"))

    resolved_api_base = (
        _clean_env(cli_api_base) or generic_api_base or openai_api_base or anthropic_api_base
    )

    if resolved_api_base:
        if is_anthropic_compatible_base_url(resolved_api_base):
            return (
                resolved_api_base,
                generic_api_key or anthropic_api_key or openai_api_key,
                False,
            )
        return (
            resolved_api_base,
            generic_api_key or openai_api_key or anthropic_api_key,
            True,
        )

    if anthropic_api_key or anthropic_api_base:
        return (
            anthropic_api_base,
            generic_api_key or anthropic_api_key or openai_api_key,
            False,
        )

    if openai_api_key or openai_api_base:
        return (
            openai_api_base,
            generic_api_key or openai_api_key or anthropic_api_key,
            True,
        )

    if generic_api_key:
        return None, generic_api_key, False

    return None, None, False
