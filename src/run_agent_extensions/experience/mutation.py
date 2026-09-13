"""Shared preflight for experience mutations.

The stores still own their file locks and domain validation. This gate is the common
host-level check so tools, review and maintenance cannot accidentally use a different
writeback rule.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from run_agent_coding.host.learning import require_writeback


class MutationRejected(RuntimeError):
    """The current mutation scope has been revoked or is not writable."""


@dataclass(frozen=True, slots=True)
class MutationContext:
    generation: str = ""
    active: bool = True
    validator: Callable[[], bool] | None = None

    def is_valid(self) -> bool:
        if not self.active:
            return False
        if self.validator is None:
            return True
        try:
            return self.validator()
        except Exception:
            return False


_current = contextvars.ContextVar[MutationContext | None]("experience_mutation", default=None)


@contextmanager
def mutation_scope(context: MutationContext | None = None) -> Iterator[None]:
    """Bind a mutation generation for one tool/maintenance operation."""
    token = _current.set(context or MutationContext())
    try:
        yield
    finally:
        _current.reset(token)


def require_mutation(action: str) -> MutationContext:
    """Apply the shared writeback and revocation checks before changing an asset."""
    require_writeback()
    context = _current.get()
    if context is not None and not context.is_valid():
        raise MutationRejected(f"{action} mutation was revoked")
    return context or MutationContext()


__all__ = ["MutationContext", "MutationRejected", "mutation_scope", "require_mutation"]
