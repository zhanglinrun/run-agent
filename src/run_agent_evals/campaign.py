"""Frozen, reproducible evaluation campaigns over isolated trials."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from run_agent_core.types import JSONValue
from run_agent_evals.evidence import repository_evidence
from run_agent_evals.models import FrozenTask, TrialArtifact
from run_agent_evals.runner import (
    EvaluationRunner,
    EvaluationSummary,
    TaskExecutor,
    reduce_trials,
    workspace_digest,
)

MANIFEST_SCHEMA = "run-agent.evaluation.campaign.v1"
REPORT_SCHEMA = "run-agent.evaluation.report.v1"


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    candidate_id: str
    seeds: tuple[int, ...] = (0,)
    concurrency: int = 1
    keep_workspaces: bool = False
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError("candidate_id must be non-empty")
        if not self.seeds:
            raise ValueError("campaign requires at least one seed")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("campaign seeds must be unique")
        if self.concurrency < 1:
            raise ValueError("campaign concurrency must be at least 1")


@dataclass(frozen=True, slots=True)
class CampaignReport:
    root: Path
    manifest_digest: str
    summary: EvaluationSummary
    trial_artifacts: tuple[Path, ...]
    inventory_digest: str
    report_digest: str


class EvaluationCampaign:
    """Run a fixed task/seed matrix and freeze enough evidence to replay reduction."""

    def __init__(self, root: str | Path, config: CampaignConfig) -> None:
        self.root = Path(root).resolve()
        self.config = config

    async def run(
        self,
        tasks: Sequence[FrozenTask],
        executor: TaskExecutor,
    ) -> CampaignReport:
        ordered_tasks = _validated_tasks(tasks)
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = _manifest_payload(ordered_tasks, self.config)
        manifest_path = self.root / "manifest.json"
        _freeze_json(manifest_path, manifest, label="campaign manifest")

        runner = EvaluationRunner(
            self.root,
            keep_workspaces=self.config.keep_workspaces,
        )
        semaphore = asyncio.Semaphore(self.config.concurrency)

        async def run_one(task: FrozenTask, seed: int) -> TrialArtifact:
            async with semaphore:
                return await runner.run_trial(
                    task,
                    executor,
                    candidate_id=self.config.candidate_id,
                    seed=seed,
                )

        trials = await asyncio.gather(
            *(run_one(task, seed) for seed in self.config.seeds for task in ordered_tasks)
        )
        return _write_report(self.root, manifest, trials)


def rebuild_campaign(root: str | Path) -> CampaignReport:
    """Recompute a campaign report from frozen manifest and trial artifacts only."""
    campaign_root = Path(root).resolve()
    manifest_path = campaign_root / "manifest.json"
    manifest = _read_object(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"unsupported campaign manifest: {manifest.get('schema')!r}")
    expected_digest = str(manifest.get("manifest_digest", ""))
    if expected_digest != _digest_without(manifest, "manifest_digest"):
        raise ValueError("campaign manifest digest does not match its content")

    loaded_trials: list[TrialArtifact] = []
    for path in sorted((campaign_root / "trials").glob("*.json")):
        trial = TrialArtifact.from_path(path)
        if trial.artifact_path is None or trial.artifact_path.resolve() != path.resolve():
            raise ValueError(f"trial artifact path receipt does not match its file: {path}")
        loaded_trials.append(trial)
    trials = tuple(loaded_trials)
    expected = {
        (str(task["id"]), int(seed))
        for seed in manifest.get("seeds", [])
        for task in manifest.get("tasks", [])
    }
    observed = {(trial.task_id, trial.seed) for trial in trials}
    if observed != expected or len(trials) != len(expected):
        raise ValueError(
            f"campaign trial matrix mismatch: expected {len(expected)}, observed {len(trials)}"
        )
    candidate_id = str(manifest.get("candidate_id", ""))
    if any(trial.candidate_id != candidate_id for trial in trials):
        raise ValueError("campaign contains a trial for a different candidate")
    return _write_report(campaign_root, manifest, trials)


def _validated_tasks(tasks: Sequence[FrozenTask]) -> tuple[FrozenTask, ...]:
    ordered = tuple(sorted(tasks, key=lambda task: task.id))
    if not ordered:
        raise ValueError("campaign requires at least one task")
    ids = [task.id for task in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("campaign task ids must be unique")
    return ordered


def _manifest_payload(
    tasks: Sequence[FrozenTask],
    config: CampaignConfig,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "repository": repository_evidence(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "candidate_id": config.candidate_id,
        "seeds": list(config.seeds),
        "concurrency": config.concurrency,
        "keep_workspaces": config.keep_workspaces,
        "metadata": config.metadata,
        "tasks": [
            {
                "id": task.id,
                "fixture": str(task.fixture.resolve()),
                "fixture_digest": workspace_digest(task.fixture),
                "prompt_sha256": _sha256(task.prompt.encode("utf-8")),
                "verify": [list(command) for command in task.verify],
                "tags": list(task.tags),
                "timeout_seconds": task.timeout_seconds,
            }
            for task in tasks
        ],
    }
    payload["manifest_digest"] = _canonical_digest(payload)
    return payload


def _write_report(
    root: Path,
    manifest: Mapping[str, Any],
    trials: Sequence[TrialArtifact],
) -> CampaignReport:
    summary = reduce_trials(trials)
    trial_paths = tuple(
        sorted(
            (trial.artifact_path for trial in trials if trial.artifact_path is not None),
            key=str,
        )
    )
    inventory = {
        path.relative_to(root).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path.read_bytes()),
        }
        for path in trial_paths
    }
    inventory_payload: dict[str, Any] = {
        "schema": "run-agent.evaluation.inventory.v1",
        "files": inventory,
    }
    inventory_payload["inventory_digest"] = _canonical_digest(inventory_payload)
    _freeze_exact(root / "inventory.json", inventory_payload, label="campaign inventory")

    report_payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "manifest_digest": manifest["manifest_digest"],
        "inventory_digest": inventory_payload["inventory_digest"],
        "summary": asdict(summary),
        "efficiency": {
            "calls_per_passed_trial": summary.calls_per_passed_trial,
            "cost_per_passed_trial": summary.cost_per_passed_trial,
        },
    }
    report_payload["report_digest"] = _canonical_digest(report_payload)
    _freeze_exact(root / "report.json", report_payload, label="campaign report")
    return CampaignReport(
        root=root,
        manifest_digest=str(manifest["manifest_digest"]),
        summary=summary,
        trial_artifacts=trial_paths,
        inventory_digest=str(inventory_payload["inventory_digest"]),
        report_digest=str(report_payload["report_digest"]),
    )


def _freeze_json(path: Path, payload: Mapping[str, Any], *, label: str) -> None:
    if path.exists():
        existing = _read_object(path)
        if _canonical_digest(existing) != _canonical_digest(payload):
            raise ValueError(f"existing {label} belongs to a different campaign")
        return
    _write_json(path, payload)


def _freeze_exact(path: Path, payload: Mapping[str, Any], *, label: str) -> None:
    if path.exists() and _canonical_digest(_read_object(path)) != _canonical_digest(payload):
        raise ValueError(f"existing {label} does not match rebuilt evidence")
    _write_json(path, payload)


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _digest_without(payload: Mapping[str, Any], key: str) -> str:
    return _canonical_digest({name: value for name, value in payload.items() if name != key})


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CampaignConfig",
    "CampaignReport",
    "EvaluationCampaign",
    "MANIFEST_SCHEMA",
    "REPORT_SCHEMA",
    "rebuild_campaign",
]
