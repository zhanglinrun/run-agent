"""Bounded LLM review that produces candidates, never direct Skill edits.

This is deliberately *not* hermes' forked review agent. There is one completion
request, with a hard output-token ceiling and an ``asyncio.wait_for`` deadline, and
the model is only ever asked for *names and reasons*:

```yaml
consolidations:
  - from: <old-skill-name>
    into: <umbrella-skill-name>
    reason: <one short sentence>
prunings:
  - name: <skill-name>
    reason: <one short sentence>
```

It never returns file content, so nothing the model says can become a Skill body by
itself. The model's answer is a *plan*; the Curator then:

* reconciles the plan with deterministic body evidence (the source name appearing in
  another Skill's text) and builds the composition itself: one ``add`` that appends a
  labeled ``## Absorbed: <source>`` section to an *existing* destination Skill, so the
  review can never invent frontmatter or a new Skill;
* submits every consolidation through ``SkillEvolution.propose``, which enforces
  ownership, pin, scope, base digest, operation and size limits, and the security scan
  before a candidate exists at all;
* archives prunings through the same protected ``.archive`` move automatic transitions
  use, and never prunes a name a consolidation mentioned.

A refused request, an unavailable provider, a timeout, an unparsable answer or a
refused proposal all land in the report as ``llm_error``/``skipped`` entries: the pass
degrades into a report, it never degrades into an unchecked write.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from run_agent_coding.host.inference import InferenceRequest, InferenceService
from run_agent_extensions.experience.candidates import CandidateOperation
from run_agent_extensions.experience.evolution import SkillEvolution
from run_agent_extensions.experience.scopes import Scope
from run_agent_extensions.experience.skill_guard import parse_frontmatter

from .config import CuratorConfig
from .library import CuratorLibrary, LibraryView, SkillRecord
from .state import epoch_to_iso
from .transitions import TransitionResult

REVIEW_PURPOSE = "curator_review"
MAX_PROMPT_RECORDS = 200
MAX_EXCERPT_CHARS = 1_200
MAX_LLM_FINAL_CHARS = 4_000
MAX_LLM_SUMMARY_CHARS = 400
MAX_REASON_CHARS = 200
REVIEW_SECTIONS = ("consolidations", "prunings")
_FENCE = re.compile(r"```ya?ml\s*\n(.*?)\n?```", re.DOTALL | re.IGNORECASE)

HYGIENE_DISCLAIMER = (
    "This report describes library hygiene only: what was marked stale, what was "
    "archived and which consolidations were proposed as candidates. It is not "
    "evidence that any Skill improved the agent's behaviour - only a passed host "
    "evaluation and an explicit publication may claim that."
)

REVIEW_SYSTEM = (
    "You review a Skill library for hygiene. Reply with prose and exactly one fenced "
    "```yaml block whose keys are `consolidations` and `prunings`, in that order:\n"
    "consolidations:\n"
    "  - from: <skill that overlaps a broader skill>\n"
    "    into: <broader skill that should absorb it>\n"
    "    reason: <one short sentence>\n"
    "prunings:\n"
    "  - name: <skill with no forward target>\n"
    "    reason: <one short sentence>\n"
    "Rules: never invent a Skill name that is not in the list; never return file "
    "content, patches or commands; leave a list empty (`consolidations: []`) when you "
    "have nothing to propose; every name may appear at most once across both lists. "
    "Archiving is the maximum destructive action this review may lead to."
)


@dataclass(frozen=True, slots=True)
class StructuredSummary:
    """The parsed ``consolidations``/``prunings`` contract."""

    consolidations: tuple[dict[str, str], ...] = ()
    prunings: tuple[dict[str, str], ...] = ()
    error: str | None = None
    incomplete: int = 0


@dataclass(frozen=True, slots=True)
class ConsolidationPlan:
    """One consolidation after deterministic reconciliation.

    ``operations`` is a single ``add`` that appends a labeled section to the *existing*
    destination Skill. The review never creates a new Skill: a consolidation that names
    an umbrella nothing can absorb into is dropped, not materialized.
    """

    name: str
    scope: Scope
    into: str
    into_scope: Scope
    reason: str
    evidence: str
    operations: tuple[CandidateOperation, ...] = ()


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """One pruning after deterministic reconciliation."""

    name: str
    scope: Scope
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """What one review request produced, before anything was written."""

    model: str = ""
    provider: str = ""
    summary: str = ""
    final: str = ""
    error: str | None = None
    consolidations: tuple[ConsolidationPlan, ...] = ()
    prunings: tuple[PrunePlan, ...] = ()
    skipped: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewApplication:
    """The result of applying one review plan."""

    consolidated: tuple[dict[str, Any], ...] = ()
    pruned: tuple[dict[str, Any], ...] = ()
    candidates: tuple[dict[str, Any], ...] = ()
    skipped: tuple[dict[str, str], ...] = ()


@dataclass(slots=True)
class _Lists:
    """The raw parse of one yaml block."""

    values: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    error: str | None = None


def parse_structured_summary(text: str) -> StructuredSummary:
    """Parse the fenced YAML contract; a bad answer yields empty lists and a reason."""
    if not text or not text.strip():
        return StructuredSummary(error="the review returned no output")
    match = _FENCE.search(text)
    if match is None:
        return StructuredSummary(error="the review returned no ```yaml block")
    parsed = _parse_lists(match.group(1))
    if parsed.error is not None:
        return StructuredSummary(error=parsed.error)
    incomplete = 0
    consolidations: list[dict[str, str]] = []
    for entry in parsed.values.get("consolidations", []):
        source = entry.get("from", "").strip()
        into = entry.get("into", "").strip()
        if not source or not into:
            incomplete += 1
            continue
        consolidations.append(
            {
                "from": source,
                "into": into,
                "reason": _clip(entry.get("reason", ""), MAX_REASON_CHARS),
            }
        )
    prunings: list[dict[str, str]] = []
    for entry in parsed.values.get("prunings", []):
        name = entry.get("name", "").strip()
        if not name:
            incomplete += 1
            continue
        prunings.append({"name": name, "reason": _clip(entry.get("reason", ""), MAX_REASON_CHARS)})
    return StructuredSummary(
        consolidations=tuple(consolidations),
        prunings=tuple(prunings),
        incomplete=incomplete,
    )


def _parse_lists(body: str) -> _Lists:
    """Read the constrained YAML subset the contract allows.

    Supporting a full YAML parser would mean a new dependency, so this accepts exactly
    the shape the prompt asks for: top-level ``<key>:`` list headers, ``- key: value``
    items and indented ``key: value`` continuations. Anything else is an error, which
    the caller reports as ``llm_error`` with empty lists.
    """
    lists = _Lists(values={section: [] for section in REVIEW_SECTIONS})
    current: str | None = None
    item: dict[str, str] | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")):
            key, separator, value = stripped.partition(":")
            name = key.strip()
            if not separator or name not in lists.values:
                lists.error = f"unparsable line in the yaml block: {stripped!r}"
                return lists
            if value.strip() not in ("", "[]"):
                lists.error = f"expected a list under {name!r} in the yaml block"
                return lists
            current = name
            item = None
            continue
        if current is None:
            lists.error = f"list item outside a known section: {stripped!r}"
            return lists
        if stripped.startswith("-"):
            item = {}
            lists.values[current].append(item)
            remainder = stripped[1:].strip()
            if remainder:
                key, separator, value = remainder.partition(":")
                if not separator:
                    lists.error = f"list item is not a mapping: {stripped!r}"
                    return lists
                item[key.strip()] = _scalar(value)
            continue
        if item is None:
            lists.error = f"stray field outside a list item: {stripped!r}"
            return lists
        key, separator, value = stripped.partition(":")
        if not separator:
            lists.error = f"unparsable field in the yaml block: {stripped!r}"
            return lists
        item[key.strip()] = _scalar(value)
    return lists


def _scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def render_candidate_list(view: LibraryView) -> str:
    """Render the bounded Skill list the review request is allowed to see."""
    if not view.records:
        return "No Skills are present in this library."
    lines = [f"Skills ({len(view.records)}):"]
    for record in view.records[:MAX_PROMPT_RECORDS]:
        consulted = epoch_to_iso(record.last_consulted_at) or "never"
        mutated = epoch_to_iso(record.last_mutation_at) or "never"
        lines.append(
            f"- name={record.name}  scope={record.scope}  "
            f"created_by={record.created_by}  state={record.state}  "
            f"pinned={'yes' if record.pinned else 'no'}  "
            f"last_consulted={consulted}  last_mutation={mutated}  "
            f"digest={record.digest[:12]}"
        )
    if len(view.records) > MAX_PROMPT_RECORDS:
        lines.append(f"... {len(view.records) - MAX_PROMPT_RECORDS} more Skills omitted")
    return "\n".join(lines)


def mentions_name(body: str, name: str) -> bool:
    """Whether a Skill body mentions ``name`` as a whole word.

    Hyphens and underscores are treated as interchangeable so ``open-webui-setup``
    matches ``open_webui_setup``, and the boundary match keeps a short name such as
    ``api`` from matching ``api-design``.
    """
    for needle in {name, name.replace("-", "_"), name.replace("_", "-")}:
        if needle and re.search(rf"(?<![0-9a-z_-]){re.escape(needle)}(?![0-9a-z_-])", body):
            return True
    return False


def plan_review(summary: StructuredSummary, *, view: LibraryView) -> ReviewOutcome:
    """Reconcile the model's plan with deterministic body evidence.

    Priority, highest first:

    1. a ``from`` that is not a live Skill, or that names itself, is dropped;
    2. a live Skill whose body mentions the source wins over the model's guess: the
       declared ``into`` is kept only when it is itself one of those Skills
       (``evidence="absorbed"``), otherwise the alphabetically first mention is used
       (``evidence="absorbed-elsewhere"``);
    3. with no body evidence at all, the declared ``into`` is kept when it exists
       (``evidence="unverified"``);
    4. otherwise the entry is dropped with a reason;
    5. a name any consolidation mentioned is never pruned in the same pass, and the
       protection rules are enforced again where the archive actually happens.
    """
    skipped: list[dict[str, str]] = []
    consolidations: list[ConsolidationPlan] = []
    mentioned: set[str] = set()
    for entry in summary.consolidations:
        source = view.by_name(entry["from"])
        if source is None:
            skipped.append({"name": entry["from"], "reason": "consolidation source is not present"})
            continue
        if entry["into"] == source.name:
            skipped.append({"name": source.name, "reason": "consolidation target is the source"})
            continue
        mentioned.add(source.name)
        hits = sorted(
            record.name
            for record in view.records
            if record.key != source.key
            and mentions_name(view.bodies.get(record.key, ""), source.name)
        )
        target = view.by_name(entry["into"])
        if hits:
            chosen = entry["into"] if entry["into"] in hits else hits[0]
            evidence = "absorbed" if chosen == entry["into"] else "absorbed-elsewhere"
            settled = view.by_name(chosen)
        elif target is not None:
            chosen = target.name
            evidence = "unverified"
            settled = target
        else:
            skipped.append(
                {
                    "name": source.name,
                    "reason": (
                        f"no Skill mentions {source.name!r} and the declared umbrella "
                        f"{entry['into']!r} does not exist"
                    ),
                }
            )
            continue
        if settled is None:
            skipped.append({"name": source.name, "reason": "consolidation target is not present"})
            continue
        operations = _compose_operations(
            source=source,
            source_body=view.bodies.get(source.key, ""),
        )
        consolidations.append(
            ConsolidationPlan(
                name=source.name,
                scope=source.scope,
                into=settled.name,
                into_scope=settled.scope,
                reason=entry["reason"],
                evidence=evidence,
                operations=operations,
            )
        )

    prunings: list[PrunePlan] = []
    seen: set[str] = set()
    for entry in summary.prunings:
        record = view.by_name(entry["name"])
        if record is None:
            skipped.append({"name": entry["name"], "reason": "pruning target is not present"})
            continue
        if record.name in seen:
            continue
        seen.add(record.name)
        if record.name in mentioned:
            skipped.append(
                {
                    "name": record.name,
                    "reason": "named in a consolidation; not pruned until the candidate lands",
                }
            )
            continue
        prunings.append(PrunePlan(record.name, record.scope, entry["reason"]))
    return ReviewOutcome(
        consolidations=tuple(consolidations),
        prunings=tuple(prunings),
        skipped=tuple(skipped),
    )


def _compose_operations(
    *,
    source: SkillRecord,
    source_body: str,
) -> tuple[CandidateOperation, ...]:
    """Build the add operation that merges one Skill into another.

    Composition rule (the only one this module implements): the destination Skill gains
    one labeled section

        ``## Absorbed: <source>``

    holding a bounded excerpt of the source's body with its frontmatter removed. The
    destination is always an existing Skill, so the append cannot invent frontmatter.
    """
    excerpt = _body_excerpt(source_body, MAX_EXCERPT_CHARS)
    section = f"\n\n## Absorbed: {source.name}\n\n{excerpt}\n"
    return (CandidateOperation("add", "", section),)


def _body_excerpt(content: str, limit: int) -> str:
    """Return a bounded, frontmatter-free excerpt of a Skill body."""
    _, body = parse_frontmatter(content)
    text = body.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    cut = clipped.rfind("\n")
    if cut > limit // 2:
        clipped = clipped[:cut]
    return clipped.rstrip()


async def run_review(
    *,
    config: CuratorConfig,
    view: LibraryView,
    inference: InferenceService,
) -> ReviewOutcome:
    """Ask the host for one bounded review completion, then plan from the answer."""
    if not inference.available:
        return ReviewOutcome(error="no InferenceService is available; the review was skipped")
    prompt = "\n\n".join(
        [
            render_candidate_list(view),
            "Propose consolidations and prunings for the Skills above. Reply with the "
            "yaml block only after a short human summary.",
        ]
    )
    try:
        result = await asyncio.wait_for(
            inference.complete(
                InferenceRequest(
                    prompt=prompt,
                    system=REVIEW_SYSTEM,
                    purpose=REVIEW_PURPOSE,
                    max_output_tokens=config.llm_review_max_output_tokens,
                )
            ),
            timeout=config.llm_review_timeout_seconds,
        )
    except TimeoutError:
        return ReviewOutcome(
            error=f"the review timed out after {config.llm_review_timeout_seconds:g}s"
        )
    except Exception as exc:  # unavailable, busy, provider failure
        return ReviewOutcome(error=f"{type(exc).__name__}: {exc}")
    summary = parse_structured_summary(result.text)
    plan = plan_review(summary, view=view)
    error = summary.error
    if error is None and summary.incomplete:
        error = f"{summary.incomplete} entry(ies) were incomplete and ignored"
    return ReviewOutcome(
        model=result.model,
        provider=str(getattr(result, "provider", "") or ""),
        summary=_clip(_prose_only(result.text) or result.text, MAX_LLM_SUMMARY_CHARS),
        final=_clip(result.text, MAX_LLM_FINAL_CHARS),
        error=error,
        consolidations=plan.consolidations,
        prunings=plan.prunings,
        skipped=plan.skipped,
    )


def _prose_only(text: str) -> str:
    """Return the model's prose with the fenced yaml block removed."""
    return _FENCE.sub("", text).strip()


async def apply_review(
    *,
    outcome: ReviewOutcome,
    library: CuratorLibrary,
    evolution: SkillEvolution | None,
    project_enabled: bool,
    source_session: str,
    source_run: str | None,
    archive_prunings: bool = True,
) -> ReviewApplication:
    """Turn a review plan into candidates and archives; never edits Skill content."""
    consolidated: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = list(outcome.skipped)

    for plan in outcome.consolidations:
        if evolution is None:
            skipped.append(
                {"name": plan.name, "reason": "the experience extension is not available"}
            )
            continue
        if not source_run:
            skipped.append(
                {
                    "name": plan.name,
                    "reason": "no completed run is recorded; a candidate needs a source run",
                }
            )
            continue
        if plan.into_scope == "project" and not project_enabled:
            skipped.append(
                {"name": plan.name, "reason": "project Skills require a trusted project"}
            )
            continue
        try:
            candidate = await evolution.propose(
                scope=plan.into_scope,
                name=plan.into,
                source_session=source_session,
                source_run=source_run,
                operations=plan.operations,
            )
        except Exception as exc:  # ownership, pin, scope, digest, scan, budgets
            skipped.append({"name": plan.name, "reason": f"candidate refused: {exc}"})
            continue
        record: dict[str, Any] = {
            "name": plan.name,
            "into": plan.into,
            "scope": plan.into_scope,
            "source": "review",
            "reason": plan.reason,
            "evidence": plan.evidence,
        }
        consolidated.append(record)
        candidates.append(
            {
                **record,
                "candidate_id": candidate.candidate_id,
                "status": candidate.status,
                "report_id": candidate.report_id,
            }
        )

    for pruning in outcome.prunings:
        if not archive_prunings:
            pruned.append(
                {
                    "name": pruning.name,
                    "scope": pruning.scope,
                    "reason": pruning.reason,
                    "archived": False,
                }
            )
            continue
        target = library.find_by_name(pruning.name)
        if target is None:
            skipped.append({"name": pruning.name, "reason": "pruning target is no longer present"})
            continue
        reason = library.skip_reason(target)
        if reason is not None:
            skipped.append({"name": target.key, "reason": f"not pruned: {reason}"})
            continue
        mutation = library.archive(
            target,
            reason=f"review pruning: {pruning.reason}" if pruning.reason else "review pruning",
        )
        if not mutation.ok:
            skipped.append({"name": target.key, "reason": mutation.message})
            continue
        pruned.append(
            {
                "name": target.name,
                "scope": target.scope,
                "reason": pruning.reason,
                "archived": True,
                "ledger_id": mutation.ledger_id,
                "moved_as": mutation.moved_as,
            }
        )
    return ReviewApplication(
        consolidated=tuple(consolidated),
        pruned=tuple(pruned),
        candidates=tuple(candidates),
        skipped=tuple(skipped),
    )


def build_run_payload(
    *,
    run_id: str,
    started_at: datetime,
    duration_seconds: float,
    dry_run: bool,
    config: CuratorConfig,
    before: tuple[SkillRecord, ...],
    after: tuple[SkillRecord, ...],
    transitions: TransitionResult,
    snapshot: tuple[dict[str, Any], ...] = (),
    snapshot_error: str | None = None,
    model: str = "",
    provider: str = "",
    llm_summary: str = "",
    llm_final: str = "",
    llm_error: str | None = None,
    consolidated: tuple[dict[str, Any], ...] = (),
    pruned: tuple[dict[str, Any], ...] = (),
    candidates: tuple[dict[str, Any], ...] = (),
    skipped: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    """Assemble the ``run.json`` document for one pass."""
    before_by_key = {record.key: record for record in before}
    after_by_key = {record.key: record for record in after}
    archived = sorted(set(before_by_key) - set(after_by_key))
    added = sorted(set(after_by_key) - set(before_by_key))
    state_transitions = [
        {"name": key, "from": before_by_key[key].state, "to": after_by_key[key].state}
        for key in sorted(set(before_by_key) & set(after_by_key))
        if before_by_key[key].state != after_by_key[key].state
    ]
    payload: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "dry_run": dry_run,
        "model": model,
        "provider": provider,
        "auto_transitions": transitions.as_json(),
        "counts": {
            "before": len(before),
            "after": len(after),
            "delta": len(after) - len(before),
            "archived_this_run": len(archived),
            "added_this_run": len(added),
            "consolidated_this_run": len(consolidated),
            "pruned_this_run": len([item for item in pruned if item.get("archived")]),
            "state_transitions": len(state_transitions),
            "skipped": len(skipped),
        },
        "archived": archived,
        "added": added,
        "consolidated": list(consolidated),
        "pruned": list(pruned),
        "candidates": list(candidates),
        "state_transitions": state_transitions,
        "skipped": list(skipped),
        "llm_summary": llm_summary,
        "llm_final": llm_final,
        "llm_error": llm_error,
        "snapshot": list(snapshot),
        "policy": config.as_json(),
    }
    if snapshot_error:
        payload["snapshot_error"] = snapshot_error
    return payload


def render_report_markdown(payload: dict[str, Any]) -> str:
    """Render the human-readable ``REPORT.md`` for one pass."""
    started = str(payload.get("started_at") or "")
    counts = payload.get("counts") or {}
    auto = payload.get("auto_transitions") or {}
    lines = [
        f"# Curator run - {started}",
        "",
        f"> {HYGIENE_DISCLAIMER}",
        "",
    ]
    if payload.get("dry_run"):
        lines += ["**Dry run** - nothing was archived, restored or written to a Skill root.", ""]
    if payload.get("llm_error"):
        lines += [f"> Review degraded: `{payload['llm_error']}`", ""]
    if payload.get("snapshot_error"):
        lines += [f"> Snapshot failed: `{payload['snapshot_error']}`", ""]
    lines += [
        "## Automatic transitions (no model)",
        "",
        f"- checked: {auto.get('checked', 0)}",
        f"- marked stale: {auto.get('marked_stale', 0)}",
        f"- archived: {auto.get('archived', 0)}",
        f"- reactivated: {auto.get('reactivated', 0)}",
        f"- applied: {'yes' if auto.get('applied') else 'no (dry run)'}",
        "",
        "## Review outcome",
        "",
        f"- model: `{payload.get('model') or '(none)'}` "
        f"via `{payload.get('provider') or '(none)'}`",
        f"- consolidations proposed as candidates: {counts.get('consolidated_this_run', 0)}",
        f"- pruned (archived): {counts.get('pruned_this_run', 0)}",
        f"- archived this run: {counts.get('archived_this_run', 0)}",
        f"- Skills before -> after: {counts.get('before', 0)} -> {counts.get('after', 0)}",
        "",
    ]
    lines += _bullet_section(
        "Consolidations (candidates only)",
        [
            f"- `{entry.get('name')}` -> `{entry.get('into')}` ({entry.get('evidence')})"
            + (f" - {entry.get('reason')}" if entry.get("reason") else "")
            for entry in payload.get("consolidated") or []
        ],
    )
    lines += _bullet_section(
        "Candidates waiting for the gate",
        [
            f"- `{entry.get('candidate_id')}` {entry.get('status')} for `{entry.get('into')}`"
            for entry in payload.get("candidates") or []
        ],
    )
    lines += _bullet_section(
        "Pruned (archived, recoverable)",
        [
            f"- `{entry.get('name')}`"
            + (f" - {entry.get('reason')}" if entry.get("reason") else "")
            for entry in payload.get("pruned") or []
            if entry.get("archived")
        ],
    )
    lines += _bullet_section(
        "Archived this run",
        [f"- `{name}`" for name in payload.get("archived") or []],
    )
    lines += _bullet_section(
        "State transitions",
        [
            f"- `{item.get('name')}`: {item.get('from')} -> {item.get('to')}"
            for item in payload.get("state_transitions") or []
        ],
    )
    lines += _bullet_section(
        "Skipped (with reason)",
        [f"- `{item.get('name')}`: {item.get('reason')}" for item in payload.get("skipped") or []],
    )
    lines += _bullet_section(
        "Snapshots taken",
        [f"- `{item.get('id')}` ({item.get('reason')})" for item in payload.get("snapshot") or []],
    )
    summary = str(payload.get("llm_summary") or "").strip()
    if summary:
        lines += ["## Review summary", "", summary, ""]
    lines += [
        "## Recovery",
        "",
        "- Restore an archived Skill: `/curator restore <skill-name>`",
        "- Restore a whole Skill root: `/curator restore <snapshot-id>`",
        "- Archived directories live under `<skills-root>/.archive/` and are moved, never deleted.",
        "- Every move is also recorded in the Skill ledger (`/evolve ledger`).",
        "",
    ]
    return "\n".join(lines)


def _bullet_section(title: str, bullets: list[str]) -> list[str]:
    if not bullets:
        return []
    return [f"## {title}", "", *bullets, ""]


def _clip(text: str, limit: int) -> str:
    flat = text.strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "\u2026"


__all__ = [
    "HYGIENE_DISCLAIMER",
    "REVIEW_PURPOSE",
    "REVIEW_SYSTEM",
    "ConsolidationPlan",
    "PrunePlan",
    "ReviewApplication",
    "ReviewOutcome",
    "StructuredSummary",
    "apply_review",
    "build_run_payload",
    "mentions_name",
    "parse_structured_summary",
    "plan_review",
    "render_candidate_list",
    "render_report_markdown",
    "run_review",
]
