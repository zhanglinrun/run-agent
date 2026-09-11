"""The persisted models.dev catalog snapshot: where it lives and what it holds.

This module answers one question - "what catalog do we have cached" - and the fetching
lives in ``models_dev_refresh``. The split exists because the two had accumulated into a
single 211-line file whose refresh routine alone ran to 92 lines, which made the steps
inside it discoverable only by reading the whole thing.

The cache helpers are public here because the refresh path needs them; they were private
only while both jobs shared a module.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

from run_agent_coding.models_dev import (
    bundled_models_dev_catalog_document,
    models_dev_catalog_overlay,
)
from run_agent_coding.paths import RunAgentPaths

MODELS_STORE_SCHEMA_VERSION = 1


def models_store_path(paths: RunAgentPaths | None = None) -> Path:
    return (paths or RunAgentPaths()).models_store_path


def cached_models_dev_catalog_document(
    paths: RunAgentPaths | None = None,
) -> dict[str, Any] | None:
    """Return a valid cache only when it is newer than the bundled snapshot."""
    cache = read_cache(paths)
    if cache is None:
        return None
    bundled = bundled_models_dev_catalog_document() or {}
    bundled_at = bundled.get("generated_at")
    cached_at = cache["catalog"].get("generated_at")
    if isinstance(bundled_at, int) and isinstance(cached_at, int) and cached_at <= bundled_at:
        return None
    return cast(dict[str, Any], cache["catalog"])


def cached_models_dev_catalog_overlay(paths: RunAgentPaths | None = None) -> dict[str, Any] | None:
    document = cached_models_dev_catalog_document(paths)
    return models_dev_catalog_overlay(document) if document is not None else None


def read_cache(paths: RunAgentPaths | None) -> dict[str, Any] | None:
    """The stored snapshot, or None when it is absent, stale-shaped or unreadable."""
    path = models_store_path(paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != MODELS_STORE_SCHEMA_VERSION
        ):
            return None
        checked_at = value.get("checked_at")
        catalog = value.get("catalog")
        if not isinstance(checked_at, int | float) or not isinstance(catalog, dict):
            return None
        models_dev_catalog_overlay(catalog)
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def model_count(document: dict[str, Any]) -> int:
    """How many models the catalog describes, counting only well-formed entries."""
    providers = document.get("providers")
    if not isinstance(providers, dict):
        return 0
    return sum(
        len(provider.get("models", []))
        for provider in providers.values()
        if isinstance(provider, dict) and isinstance(provider.get("models"), list)
    )


def write_cache(path: Path, value: dict[str, Any]) -> None:
    """Write the snapshot atomically, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(value, temp_file, indent=2, sort_keys=True)
            temp_file.write("\n")
            temp_file.flush()
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        raise
