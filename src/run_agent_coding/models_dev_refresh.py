"""Refreshing the models.dev catalog snapshot and persisting it atomically.

Split out of ``models_dev_store`` because the two jobs differ: that module answers "what
catalog do we have cached", while this one decides "should we fetch, and store what came
back". Keeping them together produced a 92-line refresh routine whose steps - offline
mode, the freshness window, the conditional request, the NVIDIA filter, validation before
persistence - were only separable by reading the whole thing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Any

import httpx

from run_agent_coding.models_dev import (
    MODELS_DEV_URL,
    NVIDIA_MODELS_URL,
    bundled_models_dev_catalog_document,
    models_dev_catalog_document,
    models_dev_catalog_overlay,
    nvidia_model_filter,
)
from run_agent_coding.models_dev_store import (
    MODELS_STORE_SCHEMA_VERSION,
    model_count,
    models_store_path,
    read_cache,
    write_cache,
)
from run_agent_coding.paths import RunAgentPaths

MODELS_REFRESH_INTERVAL_SECONDS = 4 * 60 * 60
MODELS_REFRESH_TIMEOUT_SECONDS = 15.0
USER_AGENT = "run-agent-model-catalog-refresh"
FETCH_ERRORS = (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError)


class ModelsDevRefreshError(RuntimeError):
    """Raised when a forced/runtime catalog refresh cannot complete."""


@dataclass(frozen=True, slots=True)
class ModelsDevRefreshResult:
    refreshed: bool
    not_modified: bool
    model_count: int
    cache_path: Path


def _unchanged(
    cache: dict[str, Any] | None, path: Path, *, not_modified: bool
) -> ModelsDevRefreshResult:
    """Nothing was fetched, so report what the cache already holds."""
    return ModelsDevRefreshResult(
        refreshed=False,
        not_modified=not_modified,
        model_count=model_count(cache["catalog"]) if cache is not None else 0,
        cache_path=path,
    )


def _reusable(
    cache: dict[str, Any] | None, current: float, path: Path, *, force: bool
) -> ModelsDevRefreshResult | None:
    """The offline answer, or the freshness-window answer, or None to go and fetch."""
    if environ.get("RUN_AGENT_OFFLINE") is not None:
        if cache is not None:
            return _unchanged(cache, path, not_modified=False)
        bundled = bundled_models_dev_catalog_document()
        return ModelsDevRefreshResult(
            refreshed=False,
            not_modified=False,
            model_count=model_count(bundled) if isinstance(bundled, dict) else 0,
            cache_path=path,
        )
    if not force and cache is not None:
        fresh = current - cache["checked_at"] < MODELS_REFRESH_INTERVAL_SECONDS
        if fresh:
            return _unchanged(cache, path, not_modified=False)
    return None


def _request_headers(cache: dict[str, Any] | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if cache is not None and cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    return headers


async def _fetch_nvidia_filter(http: httpx.AsyncClient, source: object) -> set[str]:
    response = await http.get(
        NVIDIA_MODELS_URL, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    return nvidia_model_filter(source, response.json())


def _build_document(source: object, nvidia: set[str], current: float) -> dict[str, Any]:
    # Imported here to avoid a catalog-loader import cycle during package startup.
    from run_agent_coding.catalog_loader import builtin_source_catalog

    return models_dev_catalog_document(
        source,
        builtin_source_catalog(),
        provider_model_filters={"nvidia": nvidia},
        generated_at=int(current * 1000),
    )


def _store(path: Path, document: dict[str, Any], current: float, etag: str | None) -> None:
    # Validate before persistence so malformed upstream data cannot poison startup.
    models_dev_catalog_overlay(document)
    write_cache(
        path,
        {
            "schema_version": MODELS_STORE_SCHEMA_VERSION,
            "checked_at": current,
            "etag": etag,
            "catalog": document,
        },
    )


async def _fetch_and_store(
    http: httpx.AsyncClient, cache: dict[str, Any] | None, path: Path, current: float
) -> ModelsDevRefreshResult:
    """One conditional refresh: fetch, validate, write, report."""
    response = await http.get(MODELS_DEV_URL, headers=_request_headers(cache))
    if response.status_code == 304 and cache is not None:
        cache["checked_at"] = current
        write_cache(path, cache)
        return _unchanged(cache, path, not_modified=True)
    response.raise_for_status()
    source = response.json()
    document = _build_document(source, await _fetch_nvidia_filter(http, source), current)
    _store(path, document, current, response.headers.get("etag"))
    return ModelsDevRefreshResult(
        refreshed=True,
        not_modified=False,
        model_count=model_count(document),
        cache_path=path,
    )


async def refresh_models_dev_catalog(
    *,
    paths: RunAgentPaths | None = None,
    force: bool = False,
    client: httpx.AsyncClient | None = None,
    now: float | None = None,
) -> ModelsDevRefreshResult:
    """Refresh models.dev plus Pi's NVIDIA filter and atomically cache the result."""
    resolved = paths or RunAgentPaths()
    path = models_store_path(resolved)
    current = now if now is not None else time.time()
    cache = read_cache(resolved)
    reusable = _reusable(cache, current, path, force=force)
    if reusable is not None:
        return reusable

    owned_client = client is None
    http = client or httpx.AsyncClient(timeout=MODELS_REFRESH_TIMEOUT_SECONDS)
    try:
        return await _fetch_and_store(http, cache, path, current)
    except FETCH_ERRORS as error:
        raise ModelsDevRefreshError(f"Could not refresh model catalogs: {error}") from error
    finally:
        if owned_client:
            await http.aclose()
