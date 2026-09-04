"""Candidate-first artifact promotion with explicit evaluation gates."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from run_agent_evals.runner import EvaluationSummary

CandidateStatus = Literal["pending", "promoted", "rejected"]


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]
    evidence: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class PromotionGate:
    min_trials: int = 5
    min_pass_rate: float = 0.8
    max_pass_rate_regression: float = 0.0
    require_same_tasks: bool = True

    def decide(
        self,
        *,
        baseline: EvaluationSummary,
        candidate: EvaluationSummary,
    ) -> PromotionDecision:
        reasons: list[str] = []
        if candidate.trials < self.min_trials:
            reasons.append(
                f"candidate has {candidate.trials} trials; requires at least {self.min_trials}"
            )
        if candidate.pass_rate < self.min_pass_rate:
            reasons.append(
                f"candidate pass rate {candidate.pass_rate:.3f} is below {self.min_pass_rate:.3f}"
            )
        regression = baseline.pass_rate - candidate.pass_rate
        if regression > self.max_pass_rate_regression + 1e-12:
            reasons.append(
                f"pass-rate regression {regression:.3f} exceeds {self.max_pass_rate_regression:.3f}"
            )
        if self.require_same_tasks and baseline.task_ids != candidate.task_ids:
            reasons.append("baseline and candidate task sets differ")
        if candidate.errored:
            reasons.append(f"candidate has {candidate.errored} errored or cancelled trials")
        return PromotionDecision(
            approved=not reasons,
            reasons=tuple(reasons),
            evidence={
                "baseline_candidate": baseline.candidate_id,
                "candidate": candidate.candidate_id,
                "baseline_trials": baseline.trials,
                "candidate_trials": candidate.trials,
                "baseline_pass_rate": baseline.pass_rate,
                "candidate_pass_rate": candidate.pass_rate,
                "pass_rate_regression": regression,
            },
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    id: str
    parent_id: str | None
    target: Path
    content_path: Path
    base_digest: str
    created_at: str
    status: CandidateStatus = "pending"
    decision: PromotionDecision | None = None


class CandidateStore:
    """Persist candidates separately and atomically promote only approved ones."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def create(self, *, target: Path, content: str, parent_id: str | None) -> Candidate:
        candidate_id = uuid4().hex
        candidate_dir = self.root / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=False)
        content_path = candidate_dir / "content.md"
        content_path.write_text(content, encoding="utf-8")
        candidate = Candidate(
            id=candidate_id,
            parent_id=parent_id,
            target=target.resolve(),
            content_path=content_path.resolve(),
            base_digest=_file_digest(target),
            created_at=datetime.now(UTC).isoformat(),
        )
        self._write(candidate)
        return candidate

    def load(self, candidate_id: str) -> Candidate:
        metadata_path = self.root / candidate_id / "candidate.json"
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        decision_payload = payload.get("decision")
        decision = (
            PromotionDecision(
                approved=bool(decision_payload["approved"]),
                reasons=tuple(decision_payload["reasons"]),
                evidence=decision_payload["evidence"],
            )
            if isinstance(decision_payload, dict)
            else None
        )
        return Candidate(
            id=str(payload["id"]),
            parent_id=payload.get("parent_id"),
            target=Path(payload["target"]),
            content_path=Path(payload["content_path"]),
            base_digest=str(payload["base_digest"]),
            created_at=str(payload["created_at"]),
            status=payload["status"],
            decision=decision,
        )

    def promote(self, candidate_id: str, decision: PromotionDecision) -> Candidate:
        candidate = self.load(candidate_id)
        if candidate.status != "pending":
            raise ValueError(f"candidate {candidate_id} is already {candidate.status}")
        if not decision.approved:
            rejected = replace(candidate, status="rejected", decision=decision)
            self._write(rejected)
            return rejected
        current_digest = _file_digest(candidate.target)
        if current_digest != candidate.base_digest:
            rejected_decision = PromotionDecision(
                approved=False,
                reasons=("target changed after candidate creation",),
                evidence=decision.evidence,
            )
            rejected = replace(candidate, status="rejected", decision=rejected_decision)
            self._write(rejected)
            return rejected
        _atomic_write(candidate.target, candidate.content_path.read_text(encoding="utf-8"))
        promoted = replace(candidate, status="promoted", decision=decision)
        self._write(promoted)
        return promoted

    def _write(self, candidate: Candidate) -> None:
        path = self.root / candidate.id / "candidate.json"
        payload = asdict(candidate)
        payload["target"] = str(candidate.target)
        payload["content_path"] = str(candidate.content_path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_digest(path: Path) -> str:
    if not path.exists():
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


__all__ = [
    "Candidate",
    "CandidateStatus",
    "CandidateStore",
    "PromotionDecision",
    "PromotionGate",
]
