"""The user/project scope shared by formal Skills and skill candidates.

This is the project's scope vocabulary, not a memory concept: Skills and their
candidates are addressed as either ``user`` (``~/.run``) or ``project``
(``<cwd>/.run``). Memory keeps its own identical scope literal in
``run_agent_extensions.hermes_memory``, where the memory stores live.
"""

from __future__ import annotations

from typing import Literal

Scope = Literal["project", "user"]

__all__ = ["Scope"]
