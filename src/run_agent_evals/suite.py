"""The task pipeline behind ``run bench suite`` (T-008, T-009).

This is the wiring the evaluation package was missing. task_spec, environment,
verifier and statistics each existed and were tested, but nothing could invoke them
from the command line: run bench still drove the old JSONL loader and campaign, so
the dual propositions and the metrics were unreachable in practice.

The suite enumerates the ready tasks in a directory, grades each one through the
dual propositions against a candidate workspace, and reduces the verdicts into a
report carrying both the per-task detail and the overall success rate with its
interval. Grading stays off the event loop, and each task is independent.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from run_agent_evals.grader_runner import GraderSuiteRunner
from run_agent_evals.ledger import LedgerSummary
from run_agent_evals.statistics import Ratio
from run_agent_evals.task_spec import TaskSpec, load_task_spec, materialize_environment
from run_agent_evals.verifier import DualPropositionVerifier, SuiteResult, classify

MANIFEST = "tasks.json"
READY = "ready"


@dataclass(frozen=True, slots=True)
class TaskVerdict:
    """What the dual propositions concluded for one task."""

    task_id: str
    succeeded: bool
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    unmet_targets: tuple[str, ...]
    newly_failing: tuple[str, ...]
    flaky: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "succeeded": self.succeeded,
            "fail_to_pass": list(self.fail_to_pass),
            "pass_to_pass": list(self.pass_to_pass),
            "unmet_targets": list(self.unmet_targets),
            "newly_failing": list(self.newly_failing),
            "flaky": list(self.flaky),
        }


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """Per-task verdicts plus the rate, interval and ledger the plan asks a report to carry."""

    verdicts: tuple[TaskVerdict, ...]
    ledger: LedgerSummary | None = None

    @property
    def rate(self) -> Ratio:
        return Ratio(sum(1 for verdict in self.verdicts if verdict.succeeded), len(self.verdicts))

    def to_json(self) -> dict[str, Any]:
        low, high = self.rate.interval()
        payload: dict[str, Any] = {
            "tasks": [verdict.to_json() for verdict in self.verdicts],
            "rate": {
                "successes": self.rate.successes,
                "trials": self.rate.trials,
                "value": self.rate.rate,
                "standard_error": self.rate.standard_error,
                "interval": [low, high],
            },
        }
        if self.ledger is not None:
            payload["ledger"] = self.ledger.to_json()
        return payload


def ready_task_ids(root: Path) -> tuple[str, ...]:
    """Ids the manifest marks ready, in manifest order."""
    document = json.loads((Path(root) / MANIFEST).read_text(encoding="utf-8"))
    return tuple(
        str(entry["id"]) for entry in document.get("tasks", ()) if entry.get("status") == READY
    )


@dataclass(frozen=True, slots=True)
class TaskSuite:
    """The ready tasks under one directory."""

    root: Path
    repeats: int = 1

    def ready(self) -> tuple[TaskSpec, ...]:
        return tuple(load_task_spec(self.root / task_id) for task_id in ready_task_ids(self.root))

    async def evaluate(self, candidate_for: Callable[[TaskSpec], Path | None]) -> SuiteReport:
        """Grade every ready task; ``candidate_for`` supplies its workspace."""
        verdicts = []
        for spec in self.ready():
            candidate = candidate_for(spec)
            if candidate is None:
                continue
            verdicts.append(await self._verdict(spec, candidate))
        return SuiteReport(tuple(verdicts))

    async def _verdict(self, spec: TaskSpec, candidate: Path) -> TaskVerdict:
        pristine = Path(candidate).parent / f".pristine-{spec.id}"
        materialize_environment(spec, pristine)
        verifier = DualPropositionVerifier(GraderSuiteRunner(spec), repeats=self.repeats)
        verdict = await verifier.verify(pristine, candidate, targets=spec.fail_to_pass)
        return TaskVerdict(
            task_id=spec.id,
            succeeded=verdict.succeeded,
            fail_to_pass=tuple(sorted(verdict.fail_to_pass)),
            pass_to_pass=tuple(sorted(verdict.pass_to_pass)),
            unmet_targets=tuple(sorted(verdict.unmet_targets)),
            newly_failing=tuple(sorted(verdict.newly_failing)),
            flaky=tuple(sorted(verdict.flaky)),
        )

    def evaluate_default(self, *, use_reference: bool) -> SuiteReport:
        """Synchronous convenience: grade each task's reference, or its pristine tree.

        Working trees are built in a temporary directory and removed afterwards. They
        must never land inside the task directory: doing so once put candidate and
        pristine copies of every task into the repository, because evals/ is not
        gitignored, and they were committed before anyone noticed.
        """
        work = Path(tempfile.mkdtemp(prefix="suite-"))
        try:
            return _run(
                self.evaluate(lambda spec: self._default_candidate(spec, use_reference, work))
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _default_candidate(self, spec: TaskSpec, use_reference: bool, work: Path) -> Path:
        candidate = work / spec.id
        materialize_environment(spec, candidate)
        if use_reference:
            for source in sorted(spec.reference.iterdir()):
                target = candidate / source.name
                target.write_bytes(source.read_bytes())
        return candidate


def _run(coroutine: Coroutine[Any, Any, SuiteReport]) -> SuiteReport:
    """Drive the async evaluation from synchronous callers such as the CLI."""
    return asyncio.run(coroutine)


def report_for_directory(root: Path, *, use_reference: bool = True) -> Mapping[str, Any]:
    """Build a report for a task directory, used by the CLI."""
    return TaskSuite(Path(root)).evaluate_default(use_reference=use_reference).to_json()


def rederive(
    pristine: Mapping[str, Mapping[str, bool]],
    candidate: Mapping[str, Mapping[str, bool]],
    targets: Mapping[str, Iterable[str]],
) -> SuiteReport:
    """Rebuild a report from stored per-test outcomes, running nothing (V06).

    A report that can only be produced by grading everything again cannot be audited,
    and chapter 7 wants the durable evidence to be sufficient. This takes what was
    persisted per test for the pristine and candidate states and replays the same
    classification: no subprocess, no model call.
    """
    verdicts = []
    for task_id in sorted(candidate):
        proposition = classify(
            SuiteResult.of_single(pristine.get(task_id, {})),
            SuiteResult.of_single(candidate[task_id]),
            targets=targets.get(task_id, ()),
        )
        verdicts.append(
            TaskVerdict(
                task_id=task_id,
                succeeded=proposition.succeeded,
                fail_to_pass=tuple(sorted(proposition.fail_to_pass)),
                pass_to_pass=tuple(sorted(proposition.pass_to_pass)),
                unmet_targets=tuple(sorted(proposition.unmet_targets)),
                newly_failing=tuple(sorted(proposition.newly_failing)),
                flaky=tuple(sorted(proposition.flaky)),
            )
        )
    return SuiteReport(tuple(verdicts))
