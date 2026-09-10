"""Whether learned experience may be written back right now (P5-4).

An evaluation run must not change the asset it is measuring, so writeback is
switched off for the duration of a measured trial and restored afterwards.

This lives in the Coding layer rather than in the experience extension because the
evaluation package ships in the wheel and ``extensions/`` does not: the flag has
to be settable without importing the extension that honours it. It is the same
mechanism Hermes uses for skill write provenance - a ContextVar, so a nested or
concurrent context cannot leak the setting.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_writeback = contextvars.ContextVar("learning_writeback", default=True)

WRITE_ORIGIN_FOREGROUND = "foreground"
WRITE_ORIGIN_REVIEW = "background_review"

_origin = contextvars.ContextVar("learning_write_origin", default=WRITE_ORIGIN_FOREGROUND)


class LearningWritebackDisabled(RuntimeError):
    """Raised when a learning write is attempted while writeback is off."""


class LearnerOwnedAsset(RuntimeError):
    """Raised when automatic maintenance would touch an asset the user asked for.

    Skills a user asks a foreground agent to write belong to the user and must never
    be consolidated, archived or pruned by the learning machinery. Only assets the
    review fork created are eligible, which is what Hermes enforces with the same
    kind of context variable.
    """


def write_origin() -> str:
    """Who is writing: the foreground learner, or the review fork."""
    return _origin.get()


def is_agent_created() -> bool:
    """True only inside the review fork, which is what may be auto-curated."""
    return _origin.get() == WRITE_ORIGIN_REVIEW


def set_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the write origin; the caller must restore the returned token."""
    return _origin.set(origin or WRITE_ORIGIN_FOREGROUND)


def reset_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the previous write origin."""
    _origin.reset(token)


@contextmanager
def review_origin() -> Iterator[None]:
    """Scope writes to the review fork for the duration of the block."""
    token = set_write_origin(WRITE_ORIGIN_REVIEW)
    try:
        yield
    finally:
        reset_write_origin(token)


def require_agent_created(agent_created: bool, action: str) -> None:
    """Refuse automatic maintenance on anything the user asked for."""
    if not agent_created:
        raise LearnerOwnedAsset(f"automatic {action} may not touch an asset the user asked for")


def writeback_enabled() -> bool:
    """Whether learned experience may be written back in this context."""
    return _writeback.get()


def require_writeback() -> None:
    """Raise unless writeback is currently permitted."""
    if not _writeback.get():
        raise LearningWritebackDisabled(
            "learning writeback is disabled while an evaluation measures this asset"
        )


def disable_writeback() -> contextvars.Token[bool]:
    """Switch writeback off; the caller must restore the returned token."""
    return _writeback.set(False)


def restore_writeback(token: contextvars.Token[bool]) -> None:
    """Restore the previous writeback state."""
    _writeback.reset(token)


@contextmanager
def writeback_disabled() -> Iterator[None]:
    """Scope writeback off for the duration of the block."""
    token = disable_writeback()
    try:
        yield
    finally:
        restore_writeback(token)
