"""Explicit model selection for reproducible evaluation campaigns."""

from __future__ import annotations

import os


def resolve_campaign_model(explicit: str | None, *, adapter_only: bool = False) -> str:
    """Resolve a model without silently choosing a provider-specific default.

    ``MODEL`` remains a backwards-compatible override.  The two named roles are
    then checked so smoke and primary runs can be configured independently.  A
    live campaign must still have a concrete model ID because the probe and the
    manifest need to identify exactly what was evaluated.
    """
    if adapter_only:
        return "adapter-only"
    for candidate in (
        explicit,
        os.environ.get("MODEL"),
        os.environ.get("MODEL_PRIMARY"),
        os.environ.get("MODEL_SMOKE"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    raise RuntimeError(
        "live evaluation requires an explicit model ID via --model, MODEL, "
        "MODEL_PRIMARY, or MODEL_SMOKE"
    )


__all__ = ["resolve_campaign_model"]
