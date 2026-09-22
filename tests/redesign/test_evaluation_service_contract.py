"""The EvaluationService host contract.

The host must expose an evaluation capability that a candidate cannot
influence: the caller freezes what is being tested, and the host - not the
caller - owns the thresholds and the grader. When no evaluation service is
registered, the capability must report itself unavailable rather than
pretending a candidate passed.
"""

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.contracts import HostServices
from run_agent_coding.host.evaluation import (
    EvaluationReport,
    EvaluationRequest,
    EvaluationUnavailable,
)
from run_agent_coding.session_manager import SessionManager


@pytest.fixture
def extension(tmp_path):
    """A minimal extension, so the host services can be reached through a session."""
    path = tmp_path / "extension.py"
    path.write_text(
        "def setup(api):\n    api.add_prompt_guideline('evaluation contract probe')\n",
        encoding="utf-8",
    )
    return path


def request(**overrides) -> EvaluationRequest:
    fields = {
        "candidate_id": "candidate-1",
        "content_hash": "sha256:abc",
        "baseline": "published-v1",
        "suite": "coding",
        "suite_version": "1",
        "budget_seconds": 60.0,
    }
    return EvaluationRequest(**{**fields, **overrides})


async def test_local_host_reports_evaluation_unavailable_not_success(tmp_path, extension):
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        evaluation = context(app).services.evaluation
        assert evaluation.available is False
        with pytest.raises(EvaluationUnavailable):
            await evaluation.submit(request())


async def test_injected_evaluation_is_published_to_extensions(tmp_path, extension):
    class Evaluation:
        available = True

        async def submit(self, frozen: EvaluationRequest) -> str:
            return frozen.candidate_id

        async def report(self, report_id: str) -> EvaluationReport:
            frozen = request(candidate_id=report_id)
            return EvaluationReport(report_id, frozen, frozen.content_hash, True, {})

    opts = replace(options(tmp_path), extension_paths=(extension,))
    manager = SessionManager(opts.paths, evaluation=Evaluation())
    try:
        async with await CodingApplication.open(
            opts, provider=ReplyProvider(), manager=manager
        ) as app:
            await app.start()
            evaluation = context(app).services.evaluation
            assert evaluation.available is True
            assert await evaluation.submit(request()) == "candidate-1"
    finally:
        await manager.aclose()


def test_evaluation_request_carries_no_caller_controlled_threshold_or_grader():
    frozen = request()
    assert frozen.content_hash == "sha256:abc"
    assert frozen.budget_seconds == 60.0
    with pytest.raises(TypeError):
        request(pass_rate=0.9)
    with pytest.raises(TypeError):
        request(grader="the candidates own tests")
    with pytest.raises(TypeError):
        request(measurement="self-reported")


def test_evaluation_report_binds_what_was_actually_measured():
    frozen = request()
    report = EvaluationReport(
        report_id="report-1",
        request=frozen,
        measured_content_hash=frozen.content_hash,
        passed=False,
        summary={"trials": 5},
    )
    assert report.measured_content_hash == frozen.content_hash
    with pytest.raises((FrozenInstanceError, AttributeError)):
        report.passed = True
    with pytest.raises((FrozenInstanceError, AttributeError)):
        report.measured_content_hash = "sha256:something-else"


def test_host_services_exposes_evaluation_beside_the_other_services():
    for name in ("tasks", "snapshots", "history", "evaluation", "inference"):
        assert hasattr(HostServices, name), name


def test_unavailable_evaluation_refuses_to_produce_a_report():
    from run_agent_coding.host.evaluation import UnavailableEvaluation

    service = UnavailableEvaluation()
    assert service.available is False
    with pytest.raises(EvaluationUnavailable):
        asyncio.run(service.report("report-1"))
    with pytest.raises(EvaluationUnavailable):
        asyncio.run(service.submit(request()))
