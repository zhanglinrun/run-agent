"""Verifier-gated Skill candidate lifecycle and publication."""

from __future__ import annotations

from dataclasses import dataclass

from run_agent_coding.host.contracts import HistoryService
from run_agent_coding.host.evaluation import (
    EvaluationReport,
    EvaluationRequest,
    EvaluationService,
)
from run_agent_coding.host.learning import writeback_enabled

from .candidates import (
    CandidateClaim,
    CandidateError,
    CandidateOperation,
    ProjectProbe,
    SkillCandidate,
    SkillCandidateStore,
    capture_claims,
)
from .config import ExperienceConfig
from .memory import MemoryScope
from .mutation import require_mutation
from .skill_manager import SkillManager, SkillWriteError, SkillWriteResult


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
    ) -> None:
        self.candidates = candidates
        self.skills = skills
        self.probe = probe
        self.evaluation = evaluation
        self.project_enabled = project_enabled
        self.policy = policy or EvolutionPolicy()
        self.config = config or ExperienceConfig()
        self.history = history

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


__all__ = ["EvolutionPolicy", "SkillEvolution"]
