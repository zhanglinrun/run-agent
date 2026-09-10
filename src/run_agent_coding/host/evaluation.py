"""Evaluation capability exposed by the Coding host.

The dividing line: a caller freezes *what* is being measured - the candidate,
its content hash, the baseline, the suite - while the host owns *how* it is
measured, meaning the thresholds, the grader and the budget enforcement. There
is deliberately no field for a requested pass rate, a candidate-supplied
grader or a self-reported measurement, so a candidate cannot grade itself.

`UnavailableEvaluation` is the local default. It reports ``available`` as false
and raises rather than pretending a candidate passed, so a caller that needs
evaluation keeps the candidate pending instead of publishing on no evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from run_agent_core.types import JSONValue


class EvaluationUnavailable(RuntimeError):
    """Raised when no evaluation service is registered for this host."""


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """A frozen candidate submitted for measurement. The host decides the rest."""

    candidate_id: str
    content_hash: str
    baseline: str
    suite: str
    suite_version: str
    budget_seconds: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """An append-only record of what one evaluation actually measured."""

    report_id: str
    request: EvaluationRequest
    measured_content_hash: str
    passed: bool
    summary: Mapping[str, JSONValue]


class EvaluationService(Protocol):
    """Host-owned evaluation. Submissions are frozen; reports are read-only."""

    @property
    def available(self) -> bool:
        """Whether this host can actually measure anything right now."""
        ...

    async def submit(self, request: EvaluationRequest) -> str:
        """Freeze the request and return a report id. Thresholds are not the caller's."""
        ...

    async def report(self, report_id: str) -> EvaluationReport:
        """Read a produced report. Reports are never rewritten."""
        ...


class UnavailableEvaluation:
    """The local default: no evaluation backend is composed into this host."""

    @property
    def available(self) -> bool:
        """Always false; nothing can be measured here."""
        return False

    async def submit(self, request: EvaluationRequest) -> str:
        """Refuse, so the caller keeps the candidate pending."""
        raise EvaluationUnavailable(
            f"no evaluation service is registered for candidate {request.candidate_id}"
        )

    async def report(self, report_id: str) -> EvaluationReport:
        """Refuse: an unavailable service cannot have produced a report."""
        raise EvaluationUnavailable(f"no evaluation service holds report {report_id}")
