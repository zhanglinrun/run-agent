"""HTTP client helpers and physical-attempt observation."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import isawaitable
from threading import RLock
from time import monotonic, time
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@dataclass(frozen=True, slots=True)
class ProviderCallContext:
    logical_call_id: str
    provider: str
    model: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class HttpAttempt:
    id: str
    logical_call_id: str | None
    provider: str | None
    model: str | None
    session_id: str | None
    method: str
    url: str
    started_at: float
    duration_ms: float
    status_code: int | None
    error_type: str | None
    request_bytes: int | None
    response_bytes: int | None
    cache_status: str | None


class HttpAttemptObserver(Protocol):
    def __call__(self, attempt: HttpAttempt) -> object: ...


_provider_call_context: ContextVar[ProviderCallContext | None] = ContextVar(
    "run_agent_provider_call_context",
    default=None,
)
_observer_lock = RLock()
_observers: list[HttpAttemptObserver] = []


def add_http_attempt_observer(observer: HttpAttemptObserver) -> Callable[[], None]:
    """Register a process-local observer and return an idempotent unsubscribe."""
    with _observer_lock:
        _observers.append(observer)
    removed = False

    def unsubscribe() -> None:
        nonlocal removed
        if removed:
            return
        removed = True
        with _observer_lock, suppress(ValueError):
            _observers.remove(observer)

    return unsubscribe


@contextmanager
def provider_call_scope(context: ProviderCallContext) -> Iterator[None]:
    token = _provider_call_context.set(context)
    try:
        yield
    finally:
        _provider_call_context.reset(token)


class ObservedAsyncClient(httpx.AsyncClient):
    """httpx client that reports each physical ``send`` attempt."""

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        started_monotonic = monotonic()
        started_at = time()
        call = _provider_call_context.get()
        response: httpx.Response | None = None
        error: BaseException | None = None
        try:
            response = await super().send(request, *args, **kwargs)
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            attempt = HttpAttempt(
                id=uuid4().hex,
                logical_call_id=call.logical_call_id if call else None,
                provider=call.provider if call else None,
                model=call.model if call else None,
                session_id=call.session_id if call else None,
                method=request.method,
                url=_safe_url(request.url),
                started_at=started_at,
                duration_ms=round((monotonic() - started_monotonic) * 1000, 3),
                status_code=response.status_code if response is not None else None,
                error_type=type(error).__name__ if error is not None else None,
                request_bytes=_content_length(request.headers),
                response_bytes=(
                    _content_length(response.headers) if response is not None else None
                ),
                cache_status=(_cache_status(response.headers) if response is not None else None),
            )
            await _notify_attempt(attempt)


async def _notify_attempt(attempt: HttpAttempt) -> None:
    with _observer_lock:
        observers = tuple(_observers)
    for observer in observers:
        try:
            result = observer(attempt)
            if isawaitable(result):
                await result
        except Exception:
            continue


def _safe_url(url: httpx.URL) -> str:
    split = urlsplit(str(url))
    host = split.hostname or ""
    if split.port is not None:
        host = f"{host}:{split.port}"
    return urlunsplit((split.scheme, host, split.path, "", ""))


def _content_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cache_status(headers: httpx.Headers) -> str | None:
    for name in ("x-cache", "cf-cache-status", "x-cache-status"):
        if value := headers.get(name):
            return str(value)
    return None


def normalize_proxy_url(proxy_url: str) -> str:
    """Return an httpx-compatible proxy URL."""
    if proxy_url.lower().startswith("socks://"):
        return f"socks5://{proxy_url[len('socks://') :]}"
    return proxy_url


@contextmanager
def normalized_proxy_environment() -> Iterator[None]:
    """Temporarily normalize proxy environment variables for httpx construction."""
    original: dict[str, str | None] = {}
    changed = False
    for name in _PROXY_ENV_VARS:
        value = os.environ.get(name)
        if value is None:
            continue
        normalized = normalize_proxy_url(value)
        if normalized == value:
            continue
        original[name] = value
        os.environ[name] = normalized
        changed = True

    try:
        yield
    finally:
        if changed:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an observed client with Run Agent proxy normalization applied."""
    with normalized_proxy_environment():
        return ObservedAsyncClient(**kwargs)


def get_json(url: str, *, timeout: float, follow_redirects: bool = False) -> dict[str, object]:
    """Fetch a JSON object with Run Agent's proxy normalization applied."""
    with normalized_proxy_environment():
        response = httpx.get(url, timeout=timeout, follow_redirects=follow_redirects)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("HTTP response must be a JSON object")
    return data


__all__ = [
    "HttpAttempt",
    "HttpAttemptObserver",
    "ObservedAsyncClient",
    "ProviderCallContext",
    "add_http_attempt_observer",
    "create_async_client",
    "get_json",
    "normalize_proxy_url",
    "normalized_proxy_environment",
    "provider_call_scope",
]
