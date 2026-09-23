"""Deterministic automatic transitions over the Skill library.

This is the pure, offline-recomputable half of the Curator: no model, no clock
beyond the ``now`` the caller passes, and no decision that depends on the previous
pass having run. The activity anchor of one Skill is

    ``last_consulted_at or last_mutation_at or mtime``

so a Skill that was read, edited or created recently is the same as one in use.
Given that anchor and the two windows:

* ``anchor <= now - archive_after_days`` -> archive the whole directory, unless a
  protection rule refuses it (the default for anything a user wrote, for the
  project scope, for pinned Skills and for protected names);
* ``anchor <= now - stale_after_days`` and the record is active -> mark it stale;
* ``anchor > now - stale_after_days`` and the record is stale or archived ->
  reactivate it.

Pinned Skills and protected names are skipped entirely, so their recorded state
never moves. Every refusal is reported in ``skipped`` with its reason instead of
being silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import CuratorConfig
from .library import CuratorLibrary, SkillRecord

SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What one automatic transition pass checked, changed or refused."""

    checked: int = 0
    marked_stale: int = 0
    archived: int = 0
    reactivated: int = 0
    skipped: tuple[dict[str, str], ...] = ()
    planned: tuple[dict[str, str], ...] = ()
    applied: bool = True

    def as_json(self) -> dict[str, Any]:
        """Return the counts stored as ``auto_transitions`` in ``run.json``."""
        return {
            "checked": self.checked,
            "marked_stale": self.marked_stale,
            "archived": self.archived,
            "reactivated": self.reactivated,
            "applied": self.applied,
            "skipped": list(self.skipped),
            "planned": list(self.planned),
        }


def apply_automatic_transitions(
    records: tuple[SkillRecord, ...],
    *,
    now: datetime,
    config: CuratorConfig,
    library: CuratorLibrary,
    apply: bool = True,
) -> TransitionResult:
    """Move every record between active/stale/archived; idempotent for a fixed ``now``.

    With ``apply=False`` the same rules are evaluated but nothing is written: the
    counters describe what *would* change and ``planned`` lists each entry, which is
    what ``/curator run --dry-run`` reports.
    """
    moment = now.timestamp()
    stale_cutoff = moment - config.stale_after_days * SECONDS_PER_DAY
    archive_cutoff = moment - config.archive_after_days * SECONDS_PER_DAY
    marked_stale = archived = reactivated = 0
    skipped: list[dict[str, str]] = []
    planned: list[dict[str, str]] = []

    for record in records:
        block = library.transition_block(record)
        if block is not None:
            skipped.append({"name": record.key, "reason": block})
            continue
        # Discovery is the proof: a Skill found in a live root is not archived, whatever
        # the stored state says (a re-created or externally restored Skill keeps a stale
        # "archived" record), so the state is normalized before the rules are applied.
        state = "active" if record.state == "archived" else record.state
        anchor = record.anchor
        if anchor <= archive_cutoff:
            reason = library.protected(record)
            if reason is not None:
                skipped.append({"name": record.key, "reason": f"not archived: {reason}"})
                continue
            detail = f"inactive for {_days(moment - anchor):.0f} days"
            planned.append({"name": record.key, "from": state, "to": "archived"})
            if apply:
                outcome = library.archive(record, reason=detail, now=now)
                if not outcome.ok:
                    skipped.append({"name": record.key, "reason": outcome.message})
                    planned.pop()
                    continue
            archived += 1
            continue
        if anchor <= stale_cutoff and state == "active":
            if apply:
                library.mark(record, "stale", now=now)
            planned.append({"name": record.key, "from": state, "to": "stale"})
            marked_stale += 1
        elif anchor > stale_cutoff and state == "stale":
            if apply:
                library.mark(record, "active", now=now)
            planned.append({"name": record.key, "from": state, "to": "active"})
            reactivated += 1

    return TransitionResult(
        checked=len(records),
        marked_stale=marked_stale,
        archived=archived,
        reactivated=reactivated,
        skipped=tuple(skipped),
        planned=tuple(planned),
        applied=apply,
    )


def _days(seconds: float) -> float:
    return seconds / SECONDS_PER_DAY


__all__ = ["TransitionResult", "apply_automatic_transitions"]
