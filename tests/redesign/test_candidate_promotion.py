"""RED: bind a candidate to a report that actually measured it (P5-3, P5-5).

A candidate may only be promoted on evidence about that candidate. The host owns
the thresholds and the grader, so the bridge cannot invent a pass, and when no
evaluation service is available the candidate stays pending rather than being
published on no evidence at all.
"""

import pytest
from extensions.experience.models import Candidate
from extensions.experience.promotion import (
    EvaluationMismatch,
    bind_report,
    require_matching_report,
)

from run_agent_coding.host.evaluation import EvaluationReport, EvaluationRequest


def candidate(**overrides) -> Candidate:
    fields = {
        "candidate_id": "candidate-1",
        "asset_id": "skill/deploy",
        "kind": "skill",
        "scope": "project",
        "base_version": "v1",
        "content_version": "v2",
        "content_hash": "sha256:aaa",
        "source_session": "session-1",
        "source_kind": "model",
        "observed_at": 1.0,
    }
    return Candidate(**{**fields, **overrides})


def report(**overrides) -> EvaluationReport:
    request = EvaluationRequest(
        candidate_id="candidate-1",
        content_hash="sha256:aaa",
        baseline="published-v1",
        suite="coding",
        suite_version="1",
        budget_seconds=60.0,
    )
    fields = {
        "report_id": "report-1",
        "request": request,
        "measured_content_hash": "sha256:aaa",
        "passed": True,
        "summary": {"trials": 5},
    }
    return EvaluationReport(**{**fields, **overrides})


def test_a_report_that_measured_other_content_is_refused() -> None:
    with pytest.raises(EvaluationMismatch, match="sha256:bbb"):
        require_matching_report(candidate(), report(measured_content_hash="sha256:bbb"))


def test_a_report_for_a_different_candidate_is_refused() -> None:
    other = EvaluationRequest(
        candidate_id="candidate-2",
        content_hash="sha256:aaa",
        baseline="published-v1",
        suite="coding",
        suite_version="1",
        budget_seconds=60.0,
    )
    with pytest.raises(EvaluationMismatch, match="candidate-2"):
        require_matching_report(candidate(), report(request=other))


def test_a_matching_passing_report_binds_its_id() -> None:
    bound = bind_report(candidate(), report())
    assert bound.report_id == "report-1"
    assert bound.status == "promoted"


def test_a_matching_failing_report_does_not_promote() -> None:
    bound = bind_report(candidate(), report(passed=False))
    assert bound.report_id == "report-1"
    assert bound.status == "rejected"


def test_binding_requires_a_report_rather_than_assuming_success() -> None:
    with pytest.raises(EvaluationMismatch):
        bind_report(candidate(), report(passed=False, measured_content_hash="sha256:zzz"))


def test_a_candidate_without_a_report_stays_unpublished() -> None:
    # Nothing in this bridge promotes on its own; promotion is bound evidence only.
    assert candidate().report_id is None
    assert candidate().status == "proposed"
