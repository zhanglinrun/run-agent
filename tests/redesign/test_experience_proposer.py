"""The run-grounded Skill proposer: one fixed run, one hard request ceiling.

The proposer is the only path where a model, not the user, decides what a Skill
candidate contains. So the tests pin three things: an unavailable inference service
produces no candidate at all, a usable answer still walks through every existing
ownership and digest gate, and a garbage answer costs exactly the declared request
ceiling instead of an unbounded retry loop.
"""

import hashlib
import json
from dataclasses import dataclass, field

import pytest
from tests.redesign.test_experience_evolution import make_evolution, skill_text

from run_agent_coding.host.evaluation import UnavailableEvaluation
from run_agent_coding.host.inference import (
    InferenceRequest,
    InferenceResult,
    UnavailableInference,
)
from run_agent_core.messages import AssistantMessage, TextContent, UserMessage
from run_agent_core.session.entries import MessageEntry, SessionEntry
from run_agent_extensions.experience.candidates import (
    CandidateError,
    ProjectProbe,
    SkillCandidateStore,
)
from run_agent_extensions.experience.evolution import (
    MAX_PROPOSER_REQUESTS,
    PROPOSER_PURPOSE,
    SkillEvolution,
)
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots


@dataclass
class FakeInference:
    """A deterministic stand-in: no provider is ever reached from a test."""

    answers: list[str] = field(default_factory=lambda: [""])
    available: bool = True
    calls: int = 0
    requests: list[InferenceRequest] = field(default_factory=list)

    async def complete(self, request: InferenceRequest) -> InferenceResult:
        self.calls += 1
        self.requests.append(request)
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        return InferenceResult(text=answer, model="fake", snapshot_id="snapshot-1")


class TranscriptHistory:
    """Returns one fixed, already committed run, the way the host history does."""

    def __init__(self, entries: tuple[SessionEntry, ...] = ()) -> None:
        self.entries = entries
        self.reads: list[str] = []

    async def read_completed_run(self, run_id: str) -> tuple[SessionEntry, ...]:
        self.reads.append(run_id)
        if run_id != "run-1":
            raise KeyError(f"Unknown or incomplete run: {run_id}")
        return self.entries


def committed_run() -> tuple[SessionEntry, ...]:
    return (
        MessageEntry(message=UserMessage(content="Run pytest before you commit.")),
        MessageEntry(
            message=AssistantMessage(
                content=[TextContent(text="Tests pass; the fix is in the SKILL.")]
            )
        ),
    )


def proposal(
    *, operations: list[dict[str, object]], claims: list[dict[str, object]] | None = None
) -> str:
    """A fenced answer, the way a real model wraps it."""
    return f"```json\n{json.dumps({'operations': operations, 'claims': claims or []})}\n```"


def harness(
    tmp_path,
    inference,
    *,
    entries: tuple[SessionEntry, ...] | None = None,
):
    """Reuse the evolution fixture and swap in the proposer's two inputs."""
    evolution, manager, project = make_evolution(tmp_path)
    evolution.inference = inference
    evolution.history = TranscriptHistory(committed_run() if entries is None else entries)
    return evolution, manager, project


def write_evolution_skill(manager, name: str = "deploy") -> str:
    directory = manager.roots.project / name
    directory.mkdir(parents=True, exist_ok=True)
    content = skill_text(name)
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return content


async def propose(evolution, name: str = "deploy"):
    return await evolution.propose_from_run(
        scope="project",
        name=name,
        source_session="session-1",
        source_run="run-1",
    )


async def test_without_a_usable_inference_service_no_candidate_is_created(tmp_path):
    evolution, manager, _ = harness(tmp_path, UnavailableInference())

    with pytest.raises(CandidateError, match="InferenceService"):
        await propose(evolution)

    unavailable = FakeInference(available=False)
    evolution.inference = unavailable
    with pytest.raises(CandidateError, match="InferenceService"):
        await propose(evolution)

    assert unavailable.calls == 0
    assert evolution.candidates.list() == []
    assert manager.find("project", "deploy") is None


async def test_a_valid_answer_becomes_one_cold_candidate_bound_to_the_source_run(tmp_path):
    evolution, manager, project = harness(tmp_path, FakeInference())
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    body = skill_text("deploy")
    inference = evolution.inference
    assert isinstance(inference, FakeInference)
    inference.answers = [
        proposal(
            operations=[{"action": "add", "new_text": body}],
            claims=[{"text": "The project configures pytest.", "probe_paths": ["pyproject.toml"]}],
        )
    ]

    candidate = await propose(evolution)

    assert evolution.candidates.list() == [candidate]
    assert candidate.source_run == "run-1" and candidate.source_session == "session-1"
    assert candidate.name == "deploy" and candidate.scope == "project"
    assert candidate.status == "cold" and candidate.report_id is None
    assert candidate.base_digest is None
    assert evolution.candidates.content(candidate) == body
    assert [claim.text for claim in candidate.claims] == ["The project configures pytest."]
    assert candidate.claims[0].probes[0].path == "pyproject.toml"
    assert manager.find("project", "deploy") is None
    assert inference.calls == 1
    assert inference.requests[0].purpose == PROPOSER_PURPOSE
    assert "Run pytest before you commit." in inference.requests[0].prompt


async def test_an_existing_skill_baseline_is_digested_into_the_candidate(tmp_path):
    evolution, manager, _ = harness(tmp_path, FakeInference())
    baseline = write_evolution_skill(manager)
    inference = evolution.inference
    assert isinstance(inference, FakeInference)
    inference.answers = [
        proposal(
            operations=[
                {
                    "action": "replace",
                    "old_text": "Run tests.",
                    "new_text": "Run tests twice.",
                }
            ]
        )
    ]

    candidate = await propose(evolution)

    assert candidate.base_digest == hashlib.sha256(baseline.encode("utf-8")).hexdigest()
    assert evolution.candidates.content(candidate) == baseline.replace(
        "Run tests.", "Run tests twice."
    )
    assert (manager.roots.project / "deploy" / "SKILL.md").read_text(encoding="utf-8") == baseline


@pytest.mark.parametrize(
    "answer",
    [
        "I am sorry, I cannot propose a Skill for that.",
        "Prose only. {not really json}",
        '{"operations": [{"action": "explode", "new_text": "x"}]}',
        proposal(operations=[{"action": "add", "new_text": "x"} for _ in range(9)]),
    ],
)
async def test_unusable_answers_spend_exactly_the_request_ceiling(tmp_path, answer):
    evolution, manager, _ = harness(tmp_path, FakeInference(answers=[answer]))
    inference = evolution.inference
    assert isinstance(inference, FakeInference)

    with pytest.raises(CandidateError, match=f"{MAX_PROPOSER_REQUESTS} inference requests"):
        await propose(evolution)

    assert inference.calls == MAX_PROPOSER_REQUESTS == 4
    assert len(inference.requests) == MAX_PROPOSER_REQUESTS
    assert inference.requests[-1].prompt.count("previous answer was rejected") == 1
    assert evolution.candidates.list() == []
    assert manager.find("project", "deploy") is None


async def test_a_failed_proposal_never_retries_past_the_ceiling(tmp_path):
    evolution, _, _ = harness(tmp_path, FakeInference(answers=["", "still nothing"]))
    inference = evolution.inference
    assert isinstance(inference, FakeInference)

    with pytest.raises(CandidateError, match="no usable proposal"):
        await propose(evolution)

    assert inference.calls <= MAX_PROPOSER_REQUESTS


async def test_a_run_without_committed_history_is_refused_before_any_request(tmp_path):
    evolution, _, _ = harness(tmp_path, FakeInference(), entries=())
    inference = evolution.inference
    assert isinstance(inference, FakeInference)

    with pytest.raises(CandidateError, match="no committed history"):
        await propose(evolution)

    assert inference.calls == 0
    assert evolution.candidates.list() == []


async def test_the_proposer_cannot_bypass_ownership_or_pin_gates(tmp_path):
    evolution, manager, _ = harness(tmp_path, FakeInference())
    directory = manager.roots.project / "deploy"
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(skill_text("deploy", owner="user"), encoding="utf-8")
    operation = {
        "action": "replace",
        "old_text": "Run tests.",
        "new_text": "Run checks.",
    }
    inference = evolution.inference
    assert isinstance(inference, FakeInference)
    inference.answers = [proposal(operations=[operation])]

    with pytest.raises(CandidateError, match="user-owned"):
        await propose(evolution)
    assert evolution.candidates.list() == []

    evolution.adopt("project", "deploy")
    manager.usage["project"].set_pinned("deploy", True)
    with pytest.raises(CandidateError, match="pinned"):
        await propose(evolution)
    assert evolution.candidates.list() == []


def test_the_constructor_defaults_to_unavailable_inference_and_accepts_one(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manager = SkillManager(SkillRoots(tmp_path / "user", tmp_path / "skills"))
    inference = FakeInference()
    evolution = SkillEvolution(
        candidates=SkillCandidateStore(tmp_path / "candidates"),
        skills=manager,
        probe=ProjectProbe(project, trusted=True),
        evaluation=UnavailableEvaluation(),
        project_enabled=True,
        inference=inference,
    )
    assert evolution.inference is inference

    defaulted = SkillEvolution(
        candidates=SkillCandidateStore(tmp_path / "other-candidates"),
        skills=manager,
        probe=ProjectProbe(project, trusted=True),
        evaluation=UnavailableEvaluation(),
        project_enabled=True,
    )
    assert isinstance(defaulted.inference, UnavailableInference)
    assert defaulted.inference.available is False
