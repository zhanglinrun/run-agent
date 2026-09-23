"""Curator: scheduled Skill-library maintenance that only ever *proposes* content.

The Curator ports hermes' skill curation (deterministic inactivity transitions, whole
library snapshots with rollback, and a bounded model review) into an extension whose
only ways to change a Skill are the ones the project already trusts:

* **archiving** moves a whole Skill directory into ``<skills-root>/.archive/`` and is
  recorded in the existing Skill ledger; nothing is ever deleted;
* **consolidations** are submitted as candidates through
  ``run_agent_extensions.experience.evolution.SkillEvolution.propose`` and therefore
  still need ownership, pin, scope, base-digest, size and security checks, host
  evaluation, and an explicit ``/evolve publish``.

Imported public surface:

``setup``
    the extension entry point registered as ``/curator``;
``CuratorConfig`` / ``load_curator_config``
    the ``CURATOR_*`` policy;
``CuratorState`` / ``CuratorStateStore``
    ``state.json``, ``usage.jsonl`` and ``reports/`` under
    ``<paths.extension_state_dir>/curator/``;
``CuratorLibrary`` / ``SkillRecord``
    the Skill records, protection rules, archive and restore;
``apply_automatic_transitions`` / ``TransitionResult``
    the deterministic stale/archive/reactivate pass;
``snapshot_skills`` / ``restore`` / ``list_snapshots``
    whole-root snapshots and rollback;
``run_review`` / ``apply_review`` / ``ReviewOutcome`` / ``ReviewApplication``
    the bounded review and candidate/prune application.
"""

from __future__ import annotations

from .config import CuratorConfig, load_curator_config
from .extension import setup
from .library import CuratorLibrary, LibraryMutation, LibraryView, SkillRecord
from .review import (
    HYGIENE_DISCLAIMER,
    ReviewApplication,
    ReviewOutcome,
    apply_review,
    build_run_payload,
    render_report_markdown,
    run_review,
)
from .snapshot import (
    SnapshotRef,
    list_snapshots,
    prune_old,
    resolve_snapshot,
    restore,
    snapshot_skills,
)
from .state import ConsultationRecord, CuratorState, CuratorStateStore, RecordState
from .transitions import TransitionResult, apply_automatic_transitions

__all__ = [
    "HYGIENE_DISCLAIMER",
    "ConsultationRecord",
    "CuratorConfig",
    "CuratorLibrary",
    "CuratorState",
    "CuratorStateStore",
    "LibraryMutation",
    "LibraryView",
    "RecordState",
    "ReviewApplication",
    "ReviewOutcome",
    "SkillRecord",
    "SnapshotRef",
    "TransitionResult",
    "apply_automatic_transitions",
    "apply_review",
    "build_run_payload",
    "list_snapshots",
    "load_curator_config",
    "prune_old",
    "render_report_markdown",
    "resolve_snapshot",
    "restore",
    "run_review",
    "setup",
    "snapshot_skills",
]
