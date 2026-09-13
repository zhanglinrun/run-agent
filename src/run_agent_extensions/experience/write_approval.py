"""Approval policy helpers for foreground experience writes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Confirm = Callable[[str, str], Awaitable[bool]]


async def approve_write(
    *,
    required: bool,
    has_ui: bool,
    confirm: Confirm | None,
    title: str,
    message: str,
) -> bool:
    """Return whether a write may proceed under the configured approval policy."""
    if not required:
        return True
    if not has_ui or confirm is None:
        return False
    return await confirm(title, message)


__all__ = ["Confirm", "approve_write"]
