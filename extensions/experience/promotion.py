"""Bridge a candidate to a report that actually measured it (P5-3, P5-5).

The host owns the thresholds and the grader, so this bridge can only bind
evidence the host returned. It cannot invent a pass, it refuses a report whose
measured content is not the candidate under review, and a candidate with no
report never becomes publishable.

Plan 6.6: publication re-checks the candidate hash, the report binding and
ownership inside one transaction. This module is the binding half of that; the
transactional half lives in the repository.
"""

from __future__ import annotations

from run_agent_coding.host.evaluation import EvaluationReport, EvaluationRequest

from .models import Candidate


class EvaluationMismatch(RuntimeError):
    """Raised when a report does not describe the candidate under review."""


def evaluation_request(
    candidate: Candidate,
    *,
    baseline: str,
    suite: str,
    suite_version: str,
    budget_seconds: float,
) -> EvaluationRequest:
    """Freeze what should be measured. Thresholds stay with the host."""
    return EvaluationRequest(
        candidate_id=candidate.candidate_id,
        content_hash=candidate.content_hash,
        baseline=baseline,
        suite=suite,
        suite_version=suite_version,
        budget_seconds=budget_seconds,
    )


def require_matching_report(candidate: Candidate, report: EvaluationReport) -> None:
    """Refuse a report that measured something other than this candidate."""
    if report.request.candidate_id != candidate.candidate_id:
        raise EvaluationMismatch(
            f"report measured {report.request.candidate_id}, not {candidate.candidate_id}"
        )
    if report.measured_content_hash != candidate.content_hash:
        raise EvaluationMismatch(
            f"report measured {report.measured_content_hash}, not {candidate.content_hash}"
        )


def bind_report(candidate: Candidate, report: EvaluationReport) -> Candidate:
    """Carry the report id on the candidate, advancing it only when it passed."""
    require_matching_report(candidate, report)
    return candidate.model_copy(
        update={
            "report_id": report.report_id,
            "status": "promoted" if report.passed else "rejected",
        }
    )
