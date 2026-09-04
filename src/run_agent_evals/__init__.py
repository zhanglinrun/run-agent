"""Reproducible evaluation artifacts, reducers, and candidate promotion."""

from run_agent_evals.campaign import (
    CampaignConfig,
    CampaignReport,
    EvaluationCampaign,
    rebuild_campaign,
)
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.evolver import (
    Candidate,
    CandidateStatus,
    CandidateStore,
    PromotionDecision,
    PromotionGate,
)
from run_agent_evals.models import (
    ExecutionResult,
    FrozenTask,
    TrialArtifact,
    TrialStatus,
    VerifierResult,
    load_tasks,
)
from run_agent_evals.runner import (
    EvaluationRunner,
    EvaluationSummary,
    TaskExecutor,
    reduce_trials,
    run_verifier,
    workspace_digest,
)
from run_agent_evals.runtime_bench import (
    RuntimeBenchmarkConfig,
    RuntimeBenchmarkReport,
    rebuild_runtime_benchmark,
    run_runtime_benchmarks,
)

__all__ = [
    "Candidate",
    "CandidateStatus",
    "CandidateStore",
    "CampaignConfig",
    "CampaignReport",
    "CodingTaskExecutor",
    "ExecutionResult",
    "EvaluationRunner",
    "EvaluationCampaign",
    "EvaluationSummary",
    "FrozenTask",
    "PromotionDecision",
    "PromotionGate",
    "RuntimeBenchmarkConfig",
    "RuntimeBenchmarkReport",
    "TaskExecutor",
    "TrialArtifact",
    "TrialStatus",
    "VerifierResult",
    "load_tasks",
    "reduce_trials",
    "rebuild_campaign",
    "rebuild_runtime_benchmark",
    "run_verifier",
    "run_runtime_benchmarks",
    "workspace_digest",
]
