"""Verifier-gated Skill candidate lifecycle and publication."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from run_agent_coding.host.contracts import HistoryService
from run_agent_coding.host.evaluation import (
    EvaluationReport,
    EvaluationRequest,
    EvaluationService,
)
from run_agent_coding.host.inference import (
    InferenceRequest,
    InferenceService,
    UnavailableInference,
)
from run_agent_coding.host.learning import writeback_enabled
from run_agent_core.messages import message_text
from run_agent_core.session.entries import (
    BranchSummaryEntry,
    CompactionEntry,
    MessageEntry,
    SessionEntry,
)

from .candidates import (
    CandidateClaim,
    CandidateError,
    CandidateOperation,
    OperationAction,
    ProjectProbe,
    SkillCandidate,
    SkillCandidateStore,
    capture_claims,
)
from .config import ExperienceConfig
from .memory import MemoryScope
from .mutation import require_mutation
from .skill_manager import SkillManager, SkillWriteError, SkillWriteResult

# The proposer reads one fixed run, so its prompt is a bounded summary and its request
# count is a hard ceiling: a retry spends budget, it never extends it.
MAX_PROPOSER_REQUESTS = 4
MAX_PROPOSER_ENTRY_CHARS = 1_200
MAX_PROPOSER_TRANSCRIPT_CHARS = 8_000
MAX_PROPOSER_SKILL_CHARS = 4_000
MAX_PROPOSER_OUTPUT_TOKENS = 1_500
MAX_PROPOSER_FEEDBACK_CHARS = 400
MAX_PROPOSER_OPERATIONS = 8
PROPOSER_PURPOSE = "experience_propose"
MISSING_SKILL_BODY = "(this Skill does not exist yet; an add operation must create it)"
PROPOSER_SYSTEM = (
    "You improve one Skill from one completed run. Reply with a single JSON object and "
    'nothing else: {"operations": [{"action": "add"|"delete"|"replace", "old_text": "...", '
    '"new_text": "..."}], "claims": [{"text": "...", "probe_paths": ["relative/path"]}]}. '
    "Use at most 8 operations and keep the total changed characters under 2000. An add "
    "without old_text appends. A replace or delete must quote old_text exactly once. Cite "
    "a project fact only when one relative file path inside the project proves it, and list "
    "that file in probe_paths: a probe path must name a regular file you read, never a "
    "directory. If the run teaches nothing reusable, reply with an empty operations list."
)


@dataclass(frozen=True, slots=True)
class EvolutionPolicy:
    suite: str = "evolution"
    suite_version: str = "1"
    budget_seconds: float = 300.0


class SkillEvolution:
    """Coordinates immutable proposals, host evaluation and atomic publication."""

    def __init__(
        self,
        *,
        candidates: SkillCandidateStore,
        skills: SkillManager,
        probe: ProjectProbe,
        evaluation: EvaluationService,
        project_enabled: bool,
        policy: EvolutionPolicy | None = None,
        config: ExperienceConfig | None = None,
        history: HistoryService | None = None,
        inference: InferenceService | None = None,
    ) -> None:
        self.candidates = candidates
        self.skills = skills
        self.probe = probe
        self.evaluation = evaluation
        self.project_enabled = project_enabled
        self.policy = policy or EvolutionPolicy()
        self.config = config or ExperienceConfig()
        self.history = history
        self.inference: InferenceService = inference or UnavailableInference()

    async def propose(
        self,
        *,
        scope: MemoryScope,
        name: str,
        source_session: str,
        source_run: str,
        operations: tuple[CandidateOperation, ...],
        claims: tuple[tuple[str, tuple[str, ...]], ...] = (),
        candidate_content: str | None = None,
    ) -> SkillCandidate:
        require_mutation("candidate")
        self._require_scope(scope)
        if self.history is not None:
            entries = await self.history.read_completed_run(source_run)
            if not entries:
                raise CandidateError(f"source run {source_run!r} has no committed history")
        base = self.skills.main_content(scope, name)
        if base is not None:
            if self.skills.is_pinned(scope, name):
                raise CandidateError(f"skill {name!r} is pinned")
            if not self.skills.is_evolution_owned(scope, name):
                raise CandidateError(
                    f"skill {name!r} is user-owned; run /evolve adopt {name} first"
                )
        else:
            for other_scope in ("user", "project"):
                if other_scope != scope and self.skills.find(other_scope, name) is not None:
                    raise CandidateError(
                        f"a skill named {name!r} already exists in the {other_scope} scope"
                    )
        evidence = capture_claims(self.probe, claims)
        # Materialization and operation budgets happen in the store; validation is repeated
        # before the blob is admitted and again inside the publication lock.
        from .candidates import materialize_operations

        materialized = materialize_operations(base or "", operations)
        if candidate_content is not None and candidate_content != materialized:
            raise CandidateError("operations do not materialize candidate_content")
        try:
            self.skills.validate_candidate(name, materialized)
        except SkillWriteError as exc:
            raise CandidateError(str(exc)) from exc
        candidate = self.candidates.create(
            scope=scope,
            name=name,
            source_session=source_session,
            source_run=source_run,
            base_content=base,
            operations=operations,
            claims=evidence,
            candidate_content=materialized,
        )
        if self.evaluation.available:
            await self.evaluate(candidate.candidate_id)
        return self.candidates.require(candidate.candidate_id)

    async def propose_from_run(
        self,
        *,
        scope: MemoryScope,
        name: str,
        source_session: str,
        source_run: str,
    ) -> SkillCandidate:
        """Ask the host's inference service for one bounded edit to one fixed run.

        This is not a second candidate pipeline. It reads the committed run, the current
        Skill and the verifier feedback, turns the model's answer into operations and
        claims, and hands both to ``propose``, so ownership, pin, base-digest, scope and
        probe admission stay in the one place that already enforces them. The request
        ceiling is a spent budget rather than a retry target: an unparsable answer costs
        one request and is fed back as feedback, so no answer can make the proposer ask
        ``MAX_PROPOSER_REQUESTS`` times or more.
        """
        require_mutation("candidate")
        self._require_scope(scope)
        if not self.inference.available:
            raise CandidateError(
                "no InferenceService is available for this host; the candidate can only stay cold"
            )
        if self.history is None:
            raise CandidateError(f"source run {source_run!r} has no committed history")
        entries = await self.history.read_completed_run(source_run)
        if not entries:
            raise CandidateError(f"source run {source_run!r} has no committed history")
        base = self.skills.main_content(scope, name)
        transcript = summarize_run(entries)
        body = (base or MISSING_SKILL_BODY)[:MAX_PROPOSER_SKILL_CHARS]
        feedback = ""
        attempts = 0
        while attempts < MAX_PROPOSER_REQUESTS:
            attempts += 1
            try:
                result = await self.inference.complete(
                    InferenceRequest(
                        prompt=_proposer_prompt(scope, name, body, transcript, feedback),
                        system=PROPOSER_SYSTEM,
                        purpose=PROPOSER_PURPOSE,
                        max_output_tokens=MAX_PROPOSER_OUTPUT_TOKENS,
                    )
                )
            except Exception as exc:
                # A refused or failed request is not a proposal; report the reason instead
                # of keeping a candidate nobody measured.
                raise CandidateError(f"inference request failed: {exc}") from exc
            try:
                operations, claims = parse_proposal(result.text)
            except CandidateError as exc:
                feedback = str(exc)[:MAX_PROPOSER_FEEDBACK_CHARS]
                continue
            return await self.propose(
                scope=scope,
                name=name,
                source_session=source_session,
                source_run=source_run,
                operations=operations,
                claims=claims,
            )
        raise CandidateError(
            f"no usable proposal after {MAX_PROPOSER_REQUESTS} inference requests: {feedback}"
        )

    async def evaluate(self, candidate_id: str) -> SkillCandidate:
        require_mutation("candidate")
        candidate = self.candidates.require(candidate_id)
        if candidate.status not in {"cold", "verified"}:
            return candidate
        if not self.evaluation.available:
            return candidate
        report_id = candidate.report_id
        if report_id is None:
            try:
                report_id = await self.evaluation.submit(self._request(candidate))
            except Exception:
                return candidate
            candidate = self.candidates.transition(
                candidate.candidate_id,
                "cold",
                reason="submitted to host evaluation",
                report_id=report_id,
            )
        try:
            report = await self.evaluation.report(report_id)
        except Exception:
            # A host may return the id before a campaign completes. No report is not a pass.
            return self.candidates.require(candidate.candidate_id)
        self._validate_report(candidate, report)
        return self.candidates.transition(
            candidate.candidate_id,
            "verified" if report.passed else "rejected",
            reason="host evaluation passed" if report.passed else "host evaluation failed",
            report_id=report.report_id,
        )

    async def publish(self, candidate_id: str) -> SkillWriteResult:
        require_mutation("skill")
        candidate = self.candidates.require(candidate_id)
        if candidate.status in {"published", "rejected", "superseded"}:
            if candidate.status == "published":
                path = self.skills.find(candidate.scope, candidate.name)
                if path is None:
                    raise CandidateError("published candidate has no installed Skill")
                return SkillWriteResult(
                    path / "SKILL.md",
                    f"Candidate {candidate.candidate_id} is already published.",
                    changed=False,
                )
            raise CandidateError(
                f"candidate {candidate.candidate_id} is {candidate.status} and cannot publish"
            )
        if not self.evaluation.available:
            raise CandidateError("no EvaluationService is available; candidate remains cold")
        if candidate.report_id is None:
            candidate = await self.evaluate(candidate.candidate_id)
        if candidate.report_id is None:
            raise CandidateError("candidate has no evaluation report and remains cold")
        try:
            report = await self.evaluation.report(candidate.report_id)
        except Exception as exc:
            raise CandidateError(
                f"evaluation report is unavailable; candidate remains {candidate.status}"
            ) from exc
        self._validate_report(candidate, report)
        if not report.passed:
            self.candidates.transition(
                candidate.candidate_id,
                "rejected",
                reason="host evaluation failed",
                report_id=report.report_id,
            )
            raise CandidateError("evaluation report did not pass")
        if candidate.status == "cold":
            candidate = self.candidates.transition(
                candidate.candidate_id,
                "verified",
                reason="passed report revalidated for publication",
                report_id=report.report_id,
            )
        content = self.candidates.content(candidate)
        self._verify_probes(candidate.claims)
        probes = [
            {"claim": claim.text, "path": evidence.path, "sha256": evidence.sha256}
            for claim in candidate.claims
            for evidence in claim.probes
        ]
        try:
            result = self.skills.publish_candidate(
                candidate.scope,
                candidate.name,
                content,
                expected_base_digest=candidate.base_digest,
                candidate_id=candidate.candidate_id,
                candidate_digest=candidate.candidate_digest,
                report_id=report.report_id,
                source_session=candidate.source_session,
                source_run=candidate.source_run,
                probes=probes,
            )
        except SkillWriteError as exc:
            raise CandidateError(str(exc)) from exc
        published = self.candidates.transition(
            candidate.candidate_id,
            "published",
            reason=f"published with ledger entry {result.ledger_id}",
            report_id=report.report_id,
        )
        self.candidates.supersede_others(published)
        return result

    def adopt(self, scope: MemoryScope, name: str) -> SkillWriteResult:
        require_mutation("skill")
        self._require_scope(scope)
        return self.skills.adopt_evolution(scope, name)

    def reject(self, candidate_id: str, reason: str) -> SkillCandidate:
        require_mutation("candidate")
        candidate = self.candidates.require(candidate_id)
        if candidate.status not in {"cold", "verified", "rejected"}:
            raise CandidateError(f"candidate {candidate_id} is already {candidate.status}")
        return self.candidates.transition(
            candidate_id,
            "rejected",
            reason=reason.strip() or "rejected by user",
        )

    def reconcile(self) -> list[str]:
        """Repair a crash after publish only when both formal digest and ledger prove it."""
        if not writeback_enabled():
            return []
        repaired: list[str] = []
        for candidate in self.candidates.list():
            if candidate.status not in {"cold", "verified"}:
                continue
            if self.skills.digest(candidate.scope, candidate.name) != candidate.candidate_digest:
                continue
            matching = next(
                (
                    entry
                    for entry in self.skills.ledger[candidate.scope].entries(skill=candidate.name)
                    if entry.action == "publish"
                    and entry.evidence.get("candidate_id") == candidate.candidate_id
                    and entry.evidence.get("candidate_digest") == candidate.candidate_digest
                ),
                None,
            )
            if matching is None:
                continue
            published = self.candidates.transition(
                candidate.candidate_id,
                "published",
                reason=f"reconciled from ledger entry {matching.id}",
                report_id=candidate.report_id,
            )
            self.candidates.supersede_others(published)
            repaired.append(candidate.candidate_id)
        return repaired

    def status_text(self) -> str:
        counts = {
            status: 0 for status in ("cold", "verified", "published", "rejected", "superseded")
        }
        for candidate in self.candidates.list():
            counts[candidate.status] += 1
        return (
            f"evolution evaluation: {'available' if self.evaluation.available else 'unavailable'}\n"
            + "candidates: "
            + ", ".join(f"{status}={count}" for status, count in counts.items())
        )

    def _request(self, candidate: SkillCandidate) -> EvaluationRequest:
        return EvaluationRequest(
            candidate_id=candidate.candidate_id,
            content_hash=candidate.candidate_digest,
            baseline=candidate.base_digest or "none",
            suite=self.policy.suite,
            suite_version=self.policy.suite_version,
            budget_seconds=self.policy.budget_seconds,
        )

    def _validate_report(self, candidate: SkillCandidate, report: EvaluationReport) -> None:
        expected = self._request(candidate)
        if report.report_id != candidate.report_id:
            raise CandidateError("evaluation report id does not match the candidate record")
        if report.request != expected:
            raise CandidateError("evaluation report request does not match the frozen candidate")
        if report.measured_content_hash != candidate.candidate_digest:
            raise CandidateError("evaluation measured a different candidate digest")

    def _verify_probes(self, claims: tuple[CandidateClaim, ...]) -> None:
        for claim in claims:
            if not claim.probes:
                raise CandidateError(f"claim has no project probe: {claim.text!r}")
            for evidence in claim.probes:
                if not self.probe.verify(evidence):
                    raise CandidateError(
                        f"project probe drifted or became unsafe: {evidence.path!r}"
                    )

    def _require_scope(self, scope: MemoryScope) -> None:
        if scope == "project" and not self.project_enabled:
            raise CandidateError("project Skills require a trusted project")


def summarize_run(entries: Sequence[SessionEntry]) -> str:
    """Render one completed run as a bounded, role-tagged transcript.

    Every entry is truncated to ``MAX_PROPOSER_ENTRY_CHARS`` and the total stops at
    ``MAX_PROPOSER_TRANSCRIPT_CHARS``, so the prompt does not grow with the run.
    """
    lines: list[str] = []
    used = 0
    for index, entry in enumerate(entries, start=1):
        text = _entry_text(entry)
        if not text:
            continue
        rendered = f"[{index}] {_entry_kind(entry)}: {text[:MAX_PROPOSER_ENTRY_CHARS]}"
        if used + len(rendered) > MAX_PROPOSER_TRANSCRIPT_CHARS:
            lines.append(f"[{index}] ... truncated at {MAX_PROPOSER_TRANSCRIPT_CHARS} characters")
            break
        lines.append(rendered)
        used += len(rendered)
    return "\n".join(lines)


def parse_proposal(
    text: str,
) -> tuple[tuple[CandidateOperation, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    """Turn one model answer into operations and claims, or refuse with a reason.

    Models wrap JSON in fences and prose, so the first balanced object is taken rather
    than demanding the whole answer be JSON. Every field is then checked, because
    "unparsable" has to include a well-formed object whose actions are nonsense.
    """
    payload = _first_json_object(text)
    if payload is None:
        raise CandidateError("the answer contained no JSON object")
    raw_operations = payload.get("operations")
    if not isinstance(raw_operations, list):
        raise CandidateError('the answer needs an "operations" list')
    if not raw_operations:
        raise CandidateError("the answer proposed no operations")
    if len(raw_operations) > MAX_PROPOSER_OPERATIONS:
        raise CandidateError(f"the answer proposed more than {MAX_PROPOSER_OPERATIONS} operations")
    operations: list[CandidateOperation] = []
    for position, item in enumerate(raw_operations, start=1):
        if not isinstance(item, dict):
            raise CandidateError(f"operation {position} is not an object")
        action = item.get("action")
        if action not in {"add", "delete", "replace"}:
            raise CandidateError(f"operation {position} has an unknown action {action!r}")
        raw_old = item.get("old_text") or ""
        raw_new = item.get("new_text") or ""
        if not isinstance(raw_old, str) or not isinstance(raw_new, str):
            raise CandidateError(f"operation {position} needs string old_text and new_text")
        operations.append(CandidateOperation(cast(OperationAction, action), raw_old, raw_new))
    raw_claims = payload.get("claims") or []
    if not isinstance(raw_claims, list):
        raise CandidateError('"claims" must be a list when present')
    claims: list[tuple[str, tuple[str, ...]]] = []
    for position, item in enumerate(raw_claims, start=1):
        if not isinstance(item, dict):
            raise CandidateError(f"claim {position} is not an object")
        claim_text = item.get("text")
        paths = item.get("probe_paths")
        if not isinstance(claim_text, str) or not claim_text.strip():
            raise CandidateError(f"claim {position} needs non-empty text")
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise CandidateError(f"claim {position} needs probe_paths as a list of strings")
        claims.append((claim_text, tuple(cast(list[str], paths))))
    return tuple(operations), tuple(claims)


def _proposer_prompt(
    scope: MemoryScope, name: str, body: str, transcript: str, feedback: str
) -> str:
    """Freeze the whole question: one Skill, one run summary, and any retry feedback."""
    sections = [
        f"Skill: {scope}/{name}",
        "Current Skill body:\n" + body,
        "Completed run summary:\n" + transcript,
    ]
    if feedback:
        sections.append("Your previous answer was rejected: " + feedback)
    return "\n\n".join(sections)


def _first_json_object(text: str) -> dict[str, object] | None:
    """Return the first balanced JSON object in ``text``, fences and prose included."""
    for match in re.finditer(r"\{", text):
        candidate = _balanced_object(text, match.start())
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return None


def _balanced_object(text: str, start: int) -> str | None:
    """Scan to the brace that closes the object opened at ``start``, ignoring strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _entry_text(entry: SessionEntry) -> str:
    """Read one entry's visible text without depending on any single entry subtype."""
    if isinstance(entry, MessageEntry):
        return message_text(entry.message)
    if isinstance(entry, (CompactionEntry, BranchSummaryEntry)):
        return entry.summary
    return ""


def _entry_kind(entry: SessionEntry) -> str:
    """Name an entry's role for the prompt, defaulting to its entry type."""
    if isinstance(entry, MessageEntry):
        return str(getattr(entry.message, "role", "message"))
    return str(getattr(entry, "type", "entry"))


__all__ = ["EvolutionPolicy", "SkillEvolution"]
__all__ = [
    "MAX_PROPOSER_REQUESTS",
    "EvolutionPolicy",
    "SkillEvolution",
    "parse_proposal",
    "summarize_run",
]
