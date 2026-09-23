"""Curator extension wiring: cadence, hooks, commands and usage capture.

The Curator is a *maintenance* extension: it keeps the Skill library clean, and every
content change it proposes still has to pass the existing experience gate. Its hooks
are deliberately cheap and idempotent, and none of them may break a session:

* ``session_start`` runs the cadence gate. On first sight it only seeds
  ``last_run_at`` and reports "deferred first run". When the interval has elapsed it
  takes a pre-run snapshot of every eligible Skill root, applies the automatic
  transitions, runs the bounded review, writes a report and stamps the state.
* ``agent_settled`` records ``last_review_run_id`` (the source run a later candidate
  cites). It never calls inference.
* ``input`` and ``tool_call`` record consultations of ``/skill:<name>`` and
  ``skill_manage(action=view)`` in ``usage.jsonl``.
* ``session_shutdown`` flushes the state.

Every handler swallows its exceptions into a diagnostic, and every destructive
command asks ``context.ui.confirm`` first. A session with no UI can therefore only run
read-only commands: ``/curator status``, ``/curator report`` and
``/curator run --dry-run``.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions import (
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    ExtensionHandler,
    InputEvent,
    ToolCallHookEvent,
)
from run_agent_coding.host.inference import InferenceService
from run_agent_coding.paths import RunAgentPaths
from run_agent_extensions.experience.candidates import ProjectProbe
from run_agent_extensions.experience.config import load_experience_config
from run_agent_extensions.experience.evolution import SkillEvolution
from run_agent_extensions.experience.mutation import MutationRejected
from run_agent_extensions.experience.scopes import Scope
from run_agent_extensions.experience.skill_manager import (
    NAME_PATTERN,
    SkillManager,
    SkillWriteError,
)
from run_agent_extensions.experience.stores import ExperienceStores

from .config import CuratorConfig, load_curator_config
from .journey import delete_node, journey_nodes, memory_entries, render_list, render_show
from .library import CuratorLibrary
from .review import (
    ReviewApplication,
    ReviewOutcome,
    apply_review,
    build_run_payload,
    render_report_markdown,
    run_review,
)
from .snapshot import list_snapshots, resolve_snapshot, restore, snapshot_skills
from .state import (
    DEFERRED_FIRST_RUN_SUMMARY,
    CuratorStateStore,
    clamp_summary,
    due_for_run,
    summarize_transitions,
    to_iso,
    utc_now,
)
from .transitions import TransitionResult, apply_automatic_transitions

logger = logging.getLogger(__name__)

CURATOR_USAGE = (
    "/curator status; /curator run [--dry-run]; /curator pause; /curator resume; "
    "/curator restore <snapshot-id|archived-skill>; /curator report [run-id]; "
    "/curator review; /curator learn <description> [--name <skill>] [--scope user|project] "
    "[--run <id>]; /curator journey list|show <id>|delete <id>"
)
PROMPT_GUIDELINE = (
    "Skill maintenance is automated: the Curator may mark unused Skills stale, archive "
    "unprotected evolution-owned Skills into `<skills-root>/.archive/`, and propose "
    "consolidation candidates. It never deletes Skill content and never edits a Skill "
    "body directly; a consolidation only becomes real through /evolve."
)
MAX_DIAGNOSTICS = 40
PRE_RUN_SNAPSHOT_REASON = "pre-curator-run"
SCOPES: tuple[Scope, ...] = ("user", "project")


class CuratorError(ValueError):
    """A Curator command refused by policy or by a missing precondition."""


@dataclass(slots=True)
class _Session:
    """Everything one Curator session resolves once, at ``session_start``."""

    config: CuratorConfig
    store: CuratorStateStore
    skills: SkillManager
    library: CuratorLibrary
    evolution: SkillEvolution | None
    inference: InferenceService
    paths: RunAgentPaths
    cwd: Path
    project_enabled: bool
    session_id: str
    diagnostics: list[str] = field(default_factory=list)


def setup(api: ExtensionAPI) -> None:
    """Register the Curator's hooks and its slash command on ``api``."""
    holder: dict[str, Any] = {"session": None}

    def resolved(context: ExtensionContext) -> _Session:
        """Resolve the session objects once and reuse them for the whole session."""
        current = holder["session"]
        if isinstance(current, _Session):
            return current
        built = _build_session(context)
        holder["session"] = built
        return built

    def current_session() -> _Session | None:
        current = holder["session"]
        return current if isinstance(current, _Session) else None

    def diagnose(current: _Session | None, where: str, exc: BaseException) -> None:
        """Record one swallowed hook failure; never re-raise into the session."""
        line = f"{where}: {type(exc).__name__}: {exc}"
        logger.debug("curator %s", line, exc_info=True)
        if current is not None:
            current.diagnostics.append(line)
            del current.diagnostics[:-MAX_DIAGNOSTICS]

    async def start(event: object, context: ExtensionContext) -> None:
        del event
        current: _Session | None = None
        try:
            current = resolved(context)
            if not current.config.enabled:
                return
            state = current.store.load()
            if state.paused:
                return
            now = utc_now()
            if state.last_run_at is None:
                state.last_run_at = to_iso(now)
                state.last_run_summary = DEFERRED_FIRST_RUN_SUMMARY
                state.last_run_summary_shown_at = to_iso(now)
                current.store.save(state)
                return
            if not due_for_run(state, now=now, interval_hours=current.config.interval_hours):
                return
            await _run_pass(current, dry_run=False)
        except Exception as exc:  # a maintenance hook must never break the session
            diagnose(current, "session_start", exc)

    async def settled(event: object, context: ExtensionContext) -> None:
        del context
        current = current_session()
        if current is None or not isinstance(event, AgentSettledEvent):
            return
        try:
            current.store.update(
                last_review_run_id=event.run_id,
                last_review_run_at=to_iso(utc_now()),
            )
        except Exception as exc:
            diagnose(current, "agent_settled", exc)

    async def on_input(event: object, context: ExtensionContext) -> None:
        del context
        current = current_session()
        if current is None or not isinstance(event, InputEvent):
            return
        try:
            name = _skill_command_name(event.text)
            if name:
                current.store.record_usage(
                    skill=name, scope=current.library.scope_of(name), source="slash_command"
                )
        except Exception as exc:
            diagnose(current, "input", exc)

    async def on_tool_call(event: object, context: ExtensionContext) -> None:
        del context
        current = current_session()
        if current is None or not isinstance(event, ToolCallHookEvent):
            return
        try:
            if event.tool_name != "skill_manage":
                return
            arguments = event.arguments
            if str(arguments.get("action") or "") != "view":
                return
            name = str(arguments.get("name") or "").strip()
            if not name:
                return
            declared = str(arguments.get("scope") or "")
            scope = declared if declared in {"user", "project"} else current.library.scope_of(name)
            current.store.record_usage(skill=name, scope=cast(Scope, scope), source="skill_tool")
        except Exception as exc:
            diagnose(current, "tool_call", exc)

    async def shutdown(event: object, context: object) -> None:
        del event, context
        current = current_session()
        if current is None:
            return
        try:
            current.store.save(current.store.load())
        except Exception as exc:
            diagnose(current, "session_shutdown", exc)

    api.on("session_start", cast(ExtensionHandler, start))
    api.on("agent_settled", cast(ExtensionHandler, settled))
    api.on("input", cast(ExtensionHandler, on_input))
    api.on("tool_call", cast(ExtensionHandler, on_tool_call))
    api.on("session_shutdown", cast(ExtensionHandler, shutdown))
    api.add_prompt_guideline(PROMPT_GUIDELINE)

    async def curator_command(args: str, context: ExtensionCommandContext) -> str:
        try:
            return await _command(args, context, resolved)
        except CuratorError as exc:
            return f"Refused: {exc}"
        except (SkillWriteError, MutationRejected, ValueError) as exc:
            return f"Refused: {exc}"

    api.register_command(
        "curator",
        curator_command,
        description=(
            "Maintain the Skill library: status, run, pause, resume, restore, report, "
            "review, learn and the unified journey view."
        ),
        usage=CURATOR_USAGE,
    )


def _build_session(context: ExtensionContext) -> _Session:
    """Resolve the Curator's stores, library and evolution facade for one session."""
    config = load_curator_config(context.environment)
    experience = load_experience_config(context.environment)
    stores = ExperienceStores.resolve(
        context.paths,
        context.cwd,
        config=experience,
        project_enabled=context.project_resources_enabled,
        session_id=context.session_id,
    )
    store = CuratorStateStore(context.paths.extension_state_dir)
    library = CuratorLibrary(
        stores.skills,
        store,
        config=config,
        project_enabled=context.project_resources_enabled,
        now=utc_now(),
    )
    inference = context.services.inference
    evolution = SkillEvolution(
        candidates=stores.candidates,
        skills=stores.skills,
        probe=ProjectProbe(context.cwd, trusted=context.project_resources_enabled),
        evaluation=context.services.evaluation,
        project_enabled=context.project_resources_enabled,
        history=context.services.history,
        inference=inference,
        config=experience,
    )
    return _Session(
        config=config,
        store=store,
        skills=stores.skills,
        library=library,
        evolution=evolution,
        inference=inference,
        paths=context.paths,
        cwd=context.cwd,
        project_enabled=context.project_resources_enabled,
        session_id=context.session_id or "",
    )


def _skill_command_name(text: str) -> str | None:
    """Return the Skill name a ``/skill:<name>`` prompt consults."""
    stripped = text.strip()
    if not stripped.startswith("/skill:"):
        return None
    head = stripped.split(maxsplit=1)[0]
    name = head.removeprefix("/skill:").strip()
    return name if NAME_PATTERN.fullmatch(name) else None


async def _run_pass(
    current: _Session,
    *,
    dry_run: bool,
    review_only: bool = False,
) -> dict[str, Any]:
    """Run one Curator pass: snapshot, transitions, review, report, state."""
    started = utc_now()
    run_id = current.store.next_run_id(started)
    before = current.library.records()
    snapshots: tuple[dict[str, Any], ...] = ()
    snapshot_error: str | None = None
    if not dry_run:
        snapshots, snapshot_error = _take_snapshots(current)
    if review_only:
        transitions = TransitionResult(checked=0, applied=False)
    else:
        transitions = apply_automatic_transitions(
            before,
            now=started,
            config=current.config,
            library=current.library,
            apply=not dry_run,
        )

    outcome = ReviewOutcome()
    application = ReviewApplication()
    if dry_run:
        summary = "skipped (dry run)"
    elif not current.config.llm_review_enabled:
        summary = "skipped (review disabled)"
    else:
        view = current.library.view()
        if not view.records:
            summary = "skipped (no Skills to review)"
        else:
            outcome = await run_review(
                config=current.config, view=view, inference=current.inference
            )
            application = await apply_review(
                outcome=outcome,
                library=current.library,
                evolution=current.evolution,
                project_enabled=current.project_enabled,
                source_session=current.session_id,
                source_run=current.store.load().last_review_run_id,
            )
            summary = outcome.summary or outcome.error or "no proposals"

    after = current.library.records()
    payload = build_run_payload(
        run_id=run_id,
        started_at=started,
        duration_seconds=(utc_now() - started).total_seconds(),
        dry_run=dry_run,
        config=current.config,
        before=before,
        after=after,
        transitions=transitions,
        snapshot=snapshots,
        snapshot_error=snapshot_error,
        model=outcome.model,
        provider=outcome.provider,
        llm_summary=summary,
        llm_final=outcome.final,
        llm_error=outcome.error,
        consolidated=application.consolidated,
        pruned=application.pruned,
        candidates=application.candidates,
        skipped=application.skipped,
    )
    directory = current.store.write_report(run_id, payload, render_report_markdown(payload))
    if directory is not None:
        payload["report_path"] = str(directory)
    _stamp_state(current, payload, started, directory=directory, move_clock=not dry_run)
    return payload


def _take_snapshots(current: _Session) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Snapshot every eligible Skill root; a failure is reported, never raised."""
    if not current.config.backup_enabled:
        return (), "snapshots are disabled by CURATOR_BACKUP_ENABLED"
    taken: list[dict[str, Any]] = []
    error: str | None = None
    for _scope, root in current.library.roots:
        try:
            reference = snapshot_skills(
                root,
                reason=PRE_RUN_SNAPSHOT_REASON,
                state_dir=current.paths.extension_state_dir,
                keep=current.config.backup_keep,
            )
        except Exception as exc:  # never block a pass on a backup problem
            error = f"{root}: {type(exc).__name__}: {exc}"
            continue
        if reference is None:
            if root.is_dir():
                error = f"{root}: snapshot failed"
            continue
        taken.append(reference.as_json())
    return tuple(taken), error


def _stamp_state(
    current: _Session,
    payload: dict[str, Any],
    started: datetime,
    *,
    directory: Path | None,
    move_clock: bool,
) -> None:
    """Update ``state.json`` after one pass; a preview does not move the clock."""
    state = current.store.load()
    if move_clock:
        state.last_run_at = to_iso(started)
        state.run_count += 1
    auto = payload.get("auto_transitions") or {}
    counts = {
        "marked_stale": int(auto.get("marked_stale") or 0),
        "archived": int(auto.get("archived") or 0),
        "reactivated": int(auto.get("reactivated") or 0),
    }
    prefix = summarize_transitions(counts, dry_run=bool(payload.get("dry_run")))
    review = str(payload.get("llm_summary") or "").strip()
    state.last_run_duration_seconds = float(payload.get("duration_seconds") or 0.0)
    state.last_run_summary = clamp_summary(f"{prefix}; review: {review or 'none'}")
    if directory is not None:
        state.last_report_path = str(directory)
    current.store.save(state)


async def _command(
    args: str,
    context: ExtensionCommandContext,
    resolve: Any,
) -> str:
    """Dispatch one ``/curator`` invocation."""
    try:
        words = shlex.split(args)
    except ValueError as exc:
        return f"Refused: {exc}"
    if not words:
        return CURATOR_USAGE
    action, *parts = words
    current: _Session = resolve(context.api.context)
    if action == "status":
        if parts:
            return CURATOR_USAGE
        return _status_text(current)
    if action == "run":
        dry_run = False
        if parts:
            if parts != ["--dry-run"]:
                return CURATOR_USAGE
            dry_run = True
        if not dry_run:
            await _confirm(
                context,
                "Run the Curator now?",
                "Automatic transitions may mark Skills stale and archive unprotected "
                "evolution-owned Skills into `<skills-root>/.archive/`, and the review "
                "may propose consolidation candidates.",
            )
        return _pass_summary(await _run_pass(current, dry_run=dry_run))
    if action in {"pause", "resume"}:
        if parts:
            return CURATOR_USAGE
        await _confirm(context, f"Curator {action}?", "This changes the maintenance schedule.")
        current.store.set_paused(action == "pause")
        return f"Curator is now {'paused' if action == 'pause' else 'running on schedule'}."
    if action == "restore" and len(parts) == 1:
        return await _restore(parts[0], context, current)
    if action == "report":
        if len(parts) > 1:
            return CURATOR_USAGE
        return _report_text(current, parts[0] if parts else None)
    if action == "review":
        if parts:
            return CURATOR_USAGE
        await _confirm(
            context,
            "Run the Curator review?",
            "The review proposes consolidation candidates through the experience gate "
            "and archives unprotected prunings.",
        )
        return _pass_summary(await _run_pass(current, dry_run=False, review_only=True))
    if action == "learn":
        return await _learn(parts, current)
    if action == "journey":
        return await _journey(parts, context, current)
    return CURATOR_USAGE


async def _learn(words: list[str], current: _Session) -> str:
    """Turn one description plus the last completed run into a candidate."""
    if not words:
        return CURATOR_USAGE
    scope: Scope = "project"
    name = ""
    run_id = ""
    rest: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in {"--name", "--scope", "--run"}:
            if index + 1 >= len(words):
                raise CuratorError(f"{word} needs a value")
            value = words[index + 1]
            if word == "--name":
                name = value
            elif word == "--run":
                run_id = value
            else:
                if value not in SCOPES:
                    raise CuratorError("--scope needs user or project")
                scope = value
            index += 2
            continue
        rest.append(word)
        index += 1
    description = " ".join(rest).strip()
    if not description:
        return CURATOR_USAGE
    if scope == "project" and not current.project_enabled:
        raise CuratorError("project inputs are untrusted in this session; pass --scope user")
    if not name:
        first = rest[0]
        if not NAME_PATTERN.fullmatch(first):
            raise CuratorError(
                "learn needs `--name <skill>`: the description's first word is not a Skill name"
            )
        name = first
    source_run = run_id or (current.store.load().last_review_run_id or "")
    if not source_run:
        raise CuratorError(
            "no completed run is recorded yet: finish one task in this session before "
            "`/curator learn`, or pass --run <id> of a committed run"
        )
    if current.evolution is None:
        raise CuratorError("the experience extension is not available")
    candidate = await current.evolution.propose_from_run(
        scope=scope,
        name=name,
        source_session=current.session_id,
        source_run=source_run,
    )
    return "\n".join(
        [
            f"Proposed candidate {candidate.candidate_id} for "
            f"{candidate.scope}/{candidate.name} from run {source_run}; "
            f"status={candidate.status}"
            + (f"; report={candidate.report_id}" if candidate.report_id else ""),
            "The Skill body is untouched; publish with `/evolve publish` once the host "
            "evaluation passes.",
        ]
    )


async def _journey(
    parts: list[str],
    context: ExtensionCommandContext,
    current: _Session,
) -> str:
    """Dispatch the read-mostly journey view: list, show and delete."""
    if not parts:
        return CURATOR_USAGE
    action, *rest = parts
    records = current.library.records()
    entries = memory_entries(current.paths, current.cwd)
    if action == "list":
        if rest:
            return CURATOR_USAGE
        return render_list(journey_nodes(records, entries))
    if action == "show" and len(rest) == 1:
        return render_show(rest[0], records=records, entries=entries, library=current.library)
    if action == "delete" and len(rest) == 1:
        node_id = rest[0]
        if node_id.startswith("memory:"):
            return delete_node(node_id, records=records, library=current.library).message
        await _confirm(
            context,
            f"Archive {node_id}?",
            "Deleting a Skill archives the whole directory into `<skills-root>/.archive/`; "
            "nothing is erased.",
        )
        mutation = delete_node(node_id, records=records, library=current.library)
        if not mutation.ok:
            return mutation.message
        return f"{mutation.message} (ledger {mutation.ledger_id or 'unavailable'})"
    return CURATOR_USAGE


async def _restore(
    identifier: str,
    context: ExtensionCommandContext,
    current: _Session,
) -> str:
    """Restore a whole Skill root from a snapshot, or one archived Skill."""
    state_dir = current.paths.extension_state_dir
    snapshot = resolve_snapshot(state_dir, identifier)
    if snapshot is not None:
        if not snapshot.root:
            raise CuratorError(f"snapshot {snapshot.id} records no Skill root; restore by hand")
        root = Path(snapshot.root)
        await _confirm(
            context,
            f"Restore {root} from snapshot {snapshot.id}?",
            "The current tree is moved aside, a pre-restore snapshot is taken and the "
            "snapshot is extracted in its place.",
        )
        chosen = current.library.scope_for_root(root)
        lock: Callable[[], AbstractContextManager[None]] | None = None
        if chosen is not None:

            def lock() -> AbstractContextManager[None]:
                return current.skills.write_scope(chosen)

        ok, message = restore(
            snapshot.id,
            root=root,
            state_dir=state_dir,
            keep=current.config.backup_keep,
            lock=lock,
        )
        if not ok:
            raise CuratorError(message)
        return message
    scope, name = _archived_address(identifier, current)
    await _confirm(
        context,
        f"Restore archived Skill {name}?",
        f"The whole directory is moved back into the {scope} Skill root from `.archive/`.",
    )
    mutation = current.library.restore(scope, name)
    if not mutation.ok:
        raise CuratorError(
            f"{mutation.message}; `{scope}` archive holds: "
            f"{', '.join(current.library.archived_names(scope)) or 'nothing'}"
        )
    return f"{mutation.message} (ledger {mutation.ledger_id or 'unavailable'})"


def _archived_address(identifier: str, current: _Session) -> tuple[Scope, str]:
    """Resolve ``<scope>/<name>`` or a bare archived name to a scope and name."""
    scope, separator, name = identifier.partition("/")
    if separator:
        if scope not in SCOPES or not name:
            raise CuratorError("restore takes a snapshot id, a Skill name, or <scope>/<name>")
        return scope, name
    for resolved_scope in ("project", "user"):
        if any(
            entry == identifier or entry.startswith(f"{identifier}-")
            for entry in current.library.archived_names(resolved_scope)
        ):
            return resolved_scope, identifier
    return "user", identifier


def _status_text(current: _Session) -> str:
    """Render the read-only status view, including the transition plan."""
    state = current.store.load()
    records = current.library.records()
    plan = apply_automatic_transitions(
        records, now=utc_now(), config=current.config, library=current.library, apply=False
    )
    by_state: dict[str, int] = {}
    by_scope: dict[str, int] = {}
    for record in records:
        by_state[record.state] = by_state.get(record.state, 0) + 1
        by_scope[record.scope] = by_scope.get(record.scope, 0) + 1
    protected = [record.key for record in records if current.library.skip_reason(record)]
    snapshots = list_snapshots(current.paths.extension_state_dir)
    lines = [
        f"enabled: {'yes' if current.config.enabled else 'no'}",
        f"paused: {'yes' if state.paused else 'no'}",
        f"run_count: {state.run_count}; last_run_at: {state.last_run_at or 'never'}",
        f"interval_hours: {current.config.interval_hours:g}; "
        f"stale_after_days: {current.config.stale_after_days:g}; "
        f"archive_after_days: {current.config.archive_after_days:g}",
        f"auto_archive_user_skills: {current.config.auto_archive_user_skills}; "
        f"auto_archive_project_skills: {current.config.auto_archive_project_skills}",
        f"skills: {len(records)} "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_scope.items())) or 'none'})",
        f"states: {', '.join(f'{k}={v}' for k, v in sorted(by_state.items())) or 'none'}",
        f"protected (never auto-archived): {', '.join(protected) or 'none'}",
        f"would change now: {plan.marked_stale} stale, {plan.archived} archived, "
        f"{plan.reactivated} reactivated, {len(plan.skipped)} skipped",
        f"snapshots: {len(snapshots)} (newest {snapshots[0].id if snapshots else 'none'})",
        f"last_report: {state.last_report_path or 'none'}",
        f"last_review_run_id: {state.last_review_run_id or 'none'}",
    ]
    if state.last_run_summary:
        lines.append(f"last_run_summary: {state.last_run_summary}")
    if current.diagnostics:
        lines.append("diagnostics: " + " | ".join(current.diagnostics[-5:]))
    return "\n".join(lines)


def _report_text(current: _Session, run_id: str | None) -> str:
    """Return one human-readable report, or the newest available one."""
    chosen = run_id or current.store.latest_report_id(current.store.load())
    if not chosen:
        return "No Curator report exists yet. Run `/curator run --dry-run` to write one."
    text = current.store.read_report(chosen)
    if text is None:
        ids = current.store.report_ids()
        hint = f" Available: {', '.join(ids[:5])}" if ids else ""
        raise CuratorError(f"no report with id {chosen!r}.{hint}")
    return text


def _pass_summary(payload: dict[str, Any]) -> str:
    """Render the one-screen result of one pass."""
    counts = payload.get("counts") or {}
    auto = payload.get("auto_transitions") or {}
    lines = [
        f"Curator pass {payload.get('run_id')}"
        f"{' (dry run)' if payload.get('dry_run') else ''}: "
        f"{auto.get('marked_stale', 0)} stale, {auto.get('archived', 0)} archived, "
        f"{auto.get('reactivated', 0)} reactivated, "
        f"{counts.get('consolidated_this_run', 0)} consolidation candidate(s), "
        f"{counts.get('pruned_this_run', 0)} pruned",
    ]
    if payload.get("llm_error"):
        lines.append(f"review degraded: {payload['llm_error']}")
    if payload.get("snapshot_error"):
        lines.append(f"snapshot: {payload['snapshot_error']}")
    skipped = payload.get("skipped") or []
    for item in skipped[:5]:
        lines.append(f"skipped {item.get('name')}: {item.get('reason')}")
    if len(skipped) > 5:
        lines.append(f"... and {len(skipped) - 5} more skipped (see the report)")
    if payload.get("report_path"):
        lines.append(f"report: {payload['report_path']}")
    return "\n".join(lines)


async def _confirm(context: ExtensionCommandContext, title: str, message: str) -> None:
    """Ask the user before any destructive action; refuse without a UI."""
    ui = context.api.context.ui
    if not await ui.confirm(title, message):
        raise CuratorError(
            "not confirmed: destructive Curator actions need an interactive confirmation; "
            "without a UI only `/curator status`, `/curator report` and "
            "`/curator run --dry-run` are available"
        )


__all__ = ["CURATOR_USAGE", "PROMPT_GUIDELINE", "CuratorError", "setup"]
