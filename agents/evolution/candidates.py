"""Candidate-first Skill evolution.

Online feedback may create a candidate, but active Skills change only after
replay, boundary and retention gates all pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import warnings

from .lifecycle import create_skill_file, evolve_skill_file, get_evolution_dir


_CANDIDATE_ID_RE = re.compile(r"candidate-[0-9a-f]{12}")
_CANDIDATE_HASH_FIELDS = (
    "schema_version",
    "proposed_action",
    "target_skill",
    "target",
    "candidate",
    "decision",
    "created_at",
    "nonce",
)


def _candidate_id(payload: dict[str, Any]) -> str:
    content = {key: payload.get(key) for key in _CANDIDATE_HASH_FIELDS}
    stable = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    return "candidate-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12]


def _root() -> Path:
    return get_evolution_dir()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass(frozen=True)
class PromotionEvidence:
    replay_pass: bool
    boundary_pass: bool
    retention_pass: bool
    score_delta: float = 0.0
    hard_failures: int = 0
    complete: bool = True

    @property
    def passed(self) -> bool:
        return (
            self.complete
            and self.replay_pass
            and self.boundary_pass
            and self.retention_pass
            and self.hard_failures == 0
        )


def stage_skill_candidate(
    *,
    candidate: dict[str, Any],
    proposed_action: str,
    target_skill: str = "",
    decision: dict[str, Any] | None = None,
    target: str = "project",
) -> dict[str, Any]:
    action = str(proposed_action or "").strip().lower()
    scope = str(target or "").strip().lower()
    if action not in {"add", "merge"}:
        return {
            "ok": False,
            "action": "candidate_rejected",
            "error": f"unsupported candidate action: {action or 'unknown'}",
        }
    if action == "merge" and not str(target_skill or "").strip():
        return {
            "ok": False,
            "action": "candidate_rejected",
            "error": "merge candidate requires target_skill",
        }
    if scope not in {"project", "user"}:
        return {
            "ok": False,
            "action": "candidate_rejected",
            "error": f"unsupported Skill target: {scope or 'unknown'}",
        }
    payload = {
        "schema_version": 1,
        "status": "pending_evaluation",
        "proposed_action": action,
        "target_skill": str(target_skill or "").strip(),
        "target": scope,
        "candidate": candidate,
        "decision": decision or {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nonce": time.time_ns(),
    }
    candidate_id = _candidate_id(payload)
    path = _root() / "candidates" / candidate_id / "candidate.json"
    while path.exists():
        payload["nonce"] = int(payload["nonce"]) + 1
        candidate_id = _candidate_id(payload)
        path = _root() / "candidates" / candidate_id / "candidate.json"
    payload["candidate_id"] = candidate_id
    _write_json(path, payload)
    return {
        "ok": True,
        "action": "candidate_staged",
        "candidate_id": candidate_id,
        "skill": target_skill or str(candidate.get("name") or ""),
        "path": str(path),
        "status": payload["status"],
    }


def list_skill_candidates(*, status: str | None = None) -> list[dict[str, Any]]:
    candidates_dir = _root() / "candidates"
    candidates: list[dict[str, Any]] = []
    if not candidates_dir.is_dir():
        return candidates
    for path in sorted(candidates_dir.glob("*/candidate.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            warnings.warn(
                f"Skipping invalid Skill candidate {path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if not isinstance(payload, dict):
            continue
        candidate_id = str(payload.get("candidate_id") or path.parent.name)
        if (
            _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
            or candidate_id != path.parent.name
            or candidate_id != _candidate_id(payload)
        ):
            warnings.warn(
                f"Skipping Skill candidate with mismatched id: {path}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        payload["candidate_id"] = candidate_id
        payload["path"] = str(path)
        candidates.append(payload)
    if status is not None:
        candidates = [
            item for item in candidates if str(item.get("status") or "") == status
        ]
    return sorted(
        candidates,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("candidate_id") or ""),
        ),
    )


def _activate_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = (
        payload.get("candidate")
        if isinstance(payload.get("candidate"), dict)
        else {}
    )
    action = str(payload.get("proposed_action") or "")
    target = str(payload.get("target") or "project")
    operation_id = str(payload.get("candidate_id") or "").strip()
    tags = candidate.get("tags")
    if not isinstance(tags, list):
        tags = []
    if action == "add":
        return create_skill_file(
            name=str(candidate.get("name") or ""),
            description=str(candidate.get("description") or ""),
            instructions=str(candidate.get("instructions") or ""),
            when_to_use=str(candidate.get("when_to_use") or ""),
            target=target,
            context="inline",
            user_invocable=False,
            evidence=str(candidate.get("evidence") or ""),
            actor="skill-evaluator",
            tags=[str(item) for item in tags],
            operation_id=operation_id,
        )
    if action == "merge":
        skill_name = str(payload.get("target_skill") or "")
        return evolve_skill_file(
            skill_name=skill_name,
            lesson=str(
                candidate.get("evidence")
                or candidate.get("description")
                or "Evidence-gated Skill candidate"
            ),
            rationale=str(candidate.get("rationale") or "Online candidate promotion"),
            target=target,
            instructions=str(candidate.get("instructions") or ""),
            description=str(candidate.get("description") or ""),
            when_to_use=str(candidate.get("when_to_use") or ""),
            tags=[str(item) for item in tags],
            actor="skill-evaluator",
            operation_id=operation_id,
        )
    return {"ok": False, "error": f"unsupported candidate action: {action or 'unknown'}"}


def promote_candidate(candidate_id: str, evidence: PromotionEvidence) -> dict[str, Any]:
    if _CANDIDATE_ID_RE.fullmatch(candidate_id) is None:
        return {
            "ok": False,
            "action": "promote",
            "error": f"invalid candidate id: {candidate_id}",
        }
    path = _root() / "candidates" / candidate_id / "candidate.json"
    if not path.exists():
        return {
            "ok": False,
            "action": "promote",
            "error": f"unknown candidate: {candidate_id}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            "action": "promote",
            "error": f"invalid candidate artifact: {type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "action": "promote",
            "error": "invalid candidate artifact: expected an object",
        }
    payload_id = str(payload.get("candidate_id") or "")
    if payload_id != candidate_id or payload_id != _candidate_id(payload):
        return {
            "ok": False,
            "action": "promote",
            "error": "invalid candidate artifact: content digest mismatch",
        }
    status = str(payload.get("status") or "")
    if status != "pending_evaluation":
        return {
            "ok": False,
            "action": "promote",
            "error": f"candidate cannot transition from {status or 'unknown'}",
            "candidate_id": candidate_id,
        }
    payload["promotion_evidence"] = asdict(evidence)
    destination = path
    if not evidence.complete:
        _write_json(destination, payload)
        return {
            "ok": True,
            "action": "evaluation_pending",
            "candidate_id": candidate_id,
            "evidence": asdict(evidence),
        }
    if not evidence.passed:
        payload["status"] = "rejected"
        _write_json(destination, payload)
        return {
            "ok": False,
            "action": "rejected",
            "candidate_id": candidate_id,
            "evidence": asdict(evidence),
        }

    try:
        activation = _activate_candidate(payload)
    except Exception as exc:
        activation = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload["activation"] = activation
    if not activation.get("ok"):
        payload["status"] = "activation_failed"
        _write_json(destination, payload)
        return {
            "ok": False,
            "action": "activation_failed",
            "candidate_id": candidate_id,
            "error": str(activation.get("error") or "candidate activation failed"),
            "evidence": asdict(evidence),
        }
    payload["status"] = "promoted"
    _write_json(destination, payload)
    return {
        "ok": True,
        "action": "promoted",
        "candidate_id": candidate_id,
        "activation": activation,
        "evidence": asdict(evidence),
    }
