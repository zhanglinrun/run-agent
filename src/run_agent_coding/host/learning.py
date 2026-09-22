"""Whether learned experience may be written back right now.

An evaluation run must not change the asset it is measuring, so writeback is
switched off for the duration of a measured trial and restored afterwards.

This lives in the Coding layer rather than in the experience extension because the
evaluation package must not import an optional extension to switch it off: the flag
has to be settable without importing the extension that honours it. It is a
ContextVar, so a nested or concurrent context cannot leak the setting.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_writeback = contextvars.ContextVar("learning_writeback", default=True)


class LearningWritebackDisabled(RuntimeError):
    """Raised when a learning write is attempted while writeback is off."""


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
