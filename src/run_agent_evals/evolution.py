"""Paired, evidence-backed evaluation for verifier-gated Skill candidates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from run_agent_coding.host.evaluation import EvaluationReport, EvaluationRequest
from run_agent_core.types import JSONValue
from run_agent_evals.coding import CodingTaskExecutor
from run_agent_evals.grader_runner import GraderSuiteRunner
from run_agent_evals.models import ExecutionCancelled, ExecutionFailure, FrozenTask
from run_agent_evals.task_spec import TaskSpec, load_task_spec, materialize_environment
from run_agent_evals.verifier import DualPropositionVerifier
from run_agent_extensions.experience.candidates import (
    CandidateError,
    ProjectProbe,
    SkillCandidate,
    SkillCandidateStore,
)
from run_agent_extensions.experience.memory import MemoryScope
from run_agent_extensions.experience.skill_manager import SkillManager, SkillWriteError

EVOLUTION_REPORT_SCHEMA = "run-agent.evolution-report.v1"
EVOLUTION_COMPARISON_SCHEMA = "run-agent.evolution-comparison.v1"
Split = Literal["train", "selection", "test"]
Arm = Literal[
    "baseline",
    "candidate",
    "no-skill",
    "static-skill",
    "ungated-revision",
    "gated-evolution",
]
EvolutionArm = Literal["no-skill", "static-skill", "ungated-revision", "gated-evolution"]
AblationName = Literal["project-probe", "behavior-gate"]
EVOLUTION_ARMS: tuple[EvolutionArm, ...] = (
    "no-skill",
    "static-skill",
    "ungated-revision",
    "gated-evolution",
)
EVOLUTION_ABLATIONS: tuple[AblationName, ...] = ("project-probe", "behavior-gate")
CANDIDATE_ARMS: frozenset[EvolutionArm] = frozenset({"ungated-revision", "gated-evolution"})
EVOLUTION_SCOPE_STATEMENT = (
    "受控结论只证明这两个冻结任务族（config 与 normalization），不外推通用 Coding 能力；"
    "本对照不预设任何提升百分比，passes/trials 全部取自冻结证据。"
)
EVOLUTION_DENOMINATOR_RULE = (
    "每个任务以全部 trial 为分母：失败、超时、空 patch 与基础设施错误都保留在分母里。"
)
EVOLUTION_GATE_RULE = (
    "产品路径的配对门禁要求 selection 无回归、至少一项改善且无基础设施错误；"
    "ungated-revision 与 -project-probe/-behavior-gate 消融只是对照，不改变产品路径。"
)

# A trial hit its own watchdog only when the elapsed time sits on its budget; a
# cancellation far below that budget (a whole campaign interrupted) stays an error.
_TRIAL_TIMEOUT_TOLERANCE_RATIO = 0.02
_TRIAL_TIMEOUT_TOLERANCE_FLOOR_SECONDS = 0.25

# One trial's own session transcript is the only evidence an escape scan reads. The
# scan never asks a model and never opens a socket: a trial whose tool calls name a
# target outside its own workspace is contaminated and can never count as a pass.
_ESCAPE_SCAN_OK = "ok"
_ESCAPE_SCAN_UNAVAILABLE = "unavailable"
_ESCAPE_TARGET_LIMIT = 6
_ESCAPE_TARGET_CHARS = 120
# Tool arguments that can name a filesystem target. ``command`` is special-cased and
# split into tokens instead of being treated as one path.
_ESCAPE_ARGUMENT_KEYS = frozenset(
    {
        "command",
        "cwd",
        "dir",
        "directory",
        "file",
        "file_path",
        "filepath",
        "glob",
        "path",
        "paths",
        "pattern",
        "target",
    }
)
_ESCAPE_TOKEN_SEPARATORS = re.compile(r"""[\s"'`|;&()<>=]+""")
_ESCAPE_TRAVERSAL_SEGMENT = re.compile(r"(^|[\\/])\.\.($|[\\/])")


@dataclass(frozen=True, slots=True)
class EvolutionSuiteTask:
    id: str
    split: Split
    topic: str
    path: Path


@dataclass(frozen=True, slots=True)
class EvolutionSuite:
    path: Path
    version: str
    family: str
    tasks: tuple[EvolutionSuiteTask, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class EvolutionTrial:
    task_id: str
    split: Split
    arm: Arm
    repeat: int
    succeeded: bool
    infrastructure_error: str | None
    duration_ms: float
    metadata: Mapping[str, JSONValue]
    timed_out: bool = False
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    unmet_targets: tuple[str, ...] = ()
    newly_failing: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    # A trial whose own session named a target outside its workspace is contaminated
    # evidence: it never counts as a pass, but it stays a trial failure rather than an
    # infrastructure error. Legacy frozen rows without the field rebuild as False.
    workspace_escape: bool = False


class TaskExecutor(Protocol):
    async def execute(
        self, spec: TaskSpec, workspace: Path, state_root: Path
    ) -> Mapping[str, JSONValue]: ...


class CodingExecutor:
    """Run the production CodingApplication with one isolated Experience root."""

    def __init__(
        self,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level_override: str | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.thinking_level_override = thinking_level_override

    async def execute(
        self, spec: TaskSpec, workspace: Path, state_root: Path
    ) -> Mapping[str, JSONValue]:
        executor = CodingTaskExecutor(
            state_root,
            provider_name=self.provider_name,
            model=self.model,
            thinking_level_override=cast(Any, self.thinking_level_override),
            trust_default="never",
        )
        task = FrozenTask(
            id=spec.id,
            fixture=spec.environment,
            prompt=spec.instruction,
            verify=(),
            tags=("evolution",),
            timeout_seconds=spec.budget_seconds,
        )
        result = await executor.execute(task, workspace)
        return result.metadata


class EvolutionEvaluationService:
    """Host-owned paired evaluator with immutable evidence and offline rebuild."""

    available = True

    def __init__(
        self,
        *,
        suite: Path,
        output_root: Path,
        candidates: SkillCandidateStore,
        skills: SkillManager,
        executor: TaskExecutor | None = None,
        repeats: int = 3,
    ) -> None:
        if repeats < 1:
            raise ValueError("Evolution repeats must be positive")
        self.suite = load_evolution_suite(suite)
        self.output_root = output_root.resolve()
        self.candidates = candidates
        self.skills = skills
        self.executor = executor or CodingExecutor()
        self.repeats = repeats

    async def submit(self, request: EvaluationRequest) -> str:
        candidate = self.candidates.require(request.candidate_id)
        self._validate_request(request, candidate)
        existing = self._existing_report(request)
        if existing is not None:
            return existing
        report_id = uuid4().hex
        root = self.output_root / report_id
        if root.exists():
            raise FileExistsError(root)
        root.mkdir(parents=True)
        report = await self._evaluate(report_id, request, candidate, root)
        _write_json(root / "report.json", report)
        inventory = _inventory(root, exclude={"inventory.json"})
        _write_json(
            root / "inventory.json", {"schema": EVOLUTION_REPORT_SCHEMA, "files": inventory}
        )
        return report_id

    async def report(self, report_id: str) -> EvaluationReport:
        root = self.output_root / report_id
        document = rebuild_evolution_report(root)
        request = EvaluationRequest(**document["request"])
        return EvaluationReport(
            report_id=report_id,
            request=request,
            measured_content_hash=str(document["measured_content_hash"]),
            passed=bool(document["passed"]),
            summary=cast(Mapping[str, JSONValue], document["summary"]),
        )

    async def _evaluate(
        self,
        report_id: str,
        request: EvaluationRequest,
        candidate: SkillCandidate,
        root: Path,
    ) -> dict[str, Any]:
        baseline_content = self.skills.main_content(candidate.scope, candidate.name)
        candidate_content = self.candidates.content(candidate)
        if _digest_optional(baseline_content) != candidate.base_digest:
            raise ValueError("formal Skill no longer matches the candidate baseline")
        if _sha256(candidate_content.encode()) != request.content_hash:
            raise ValueError("candidate content hash changed before evaluation")

        trials: list[EvolutionTrial] = []
        for arm, content in (("baseline", baseline_content), ("candidate", candidate_content)):
            state_root = root / "state" / arm
            _install_skill(state_root, candidate.name, content)
            measured = _installed_digest(state_root, candidate.name)
            expected = candidate.base_digest if arm == "baseline" else candidate.candidate_digest
            if measured != expected:
                raise ValueError(f"{arm} Skill digest mismatch before execution")
            for task in self.suite.tasks:
                if task.split == "train":
                    continue
                spec = load_task_spec(task.path)
                for repeat in range(self.repeats):
                    trials.append(
                        await self._run_trial(
                            cast(Arm, arm),
                            task,
                            spec,
                            repeat,
                            state_root,
                            root,
                            skill_name=candidate.name,
                            skill_digest=expected,
                            invoke_skill=content is not None,
                            budget_seconds=min(spec.budget_seconds, request.budget_seconds),
                        )
                    )

        trial_rows = [_trial_json(trial) for trial in trials]
        _write_json(root / "trials.json", trial_rows)
        summary = reduce_evolution_trials(trials, repeats=self.repeats)
        passed = bool(summary["selection_gate"]["passed"])
        source = {
            "suite": str(self.suite.path),
            "suite_digest": self.suite.digest,
            "family": self.suite.family,
            "version": self.suite.version,
            "repeats": self.repeats,
            "candidate_id": candidate.candidate_id,
            "candidate_digest": candidate.candidate_digest,
            "baseline_digest": candidate.base_digest,
            "skill": candidate.name,
            "scope": candidate.scope,
            "task_digests": {task.id: _directory_digest(task.path) for task in self.suite.tasks},
        }
        _write_json(root / "source.json", source)
        return {
            "schema": EVOLUTION_REPORT_SCHEMA,
            "report_id": report_id,
            "request": asdict(request),
            "measured_content_hash": candidate.candidate_digest,
            "passed": passed,
            "summary": summary,
            "source": source,
        }

    async def _run_trial(
        self,
        arm: Arm,
        task: EvolutionSuiteTask,
        spec: TaskSpec,
        repeat: int,
        state_root: Path,
        report_root: Path,
        *,
        skill_name: str,
        skill_digest: str | None,
        invoke_skill: bool,
        budget_seconds: float,
    ) -> EvolutionTrial:
        return await _run_trial(
            self.executor,
            arm,
            task,
            spec,
            repeat,
            state_root,
            report_root,
            skill_name=skill_name,
            skill_digest=skill_digest,
            invoke_skill=invoke_skill,
            budget_seconds=budget_seconds,
        )

    def _validate_request(self, request: EvaluationRequest, candidate: SkillCandidate) -> None:
        if request.content_hash != candidate.candidate_digest:
            raise ValueError("request content hash does not match candidate")
        if request.baseline != (candidate.base_digest or "none"):
            raise ValueError("request baseline does not match candidate")
        if request.suite != self.suite.family or request.suite_version != self.suite.version:
            raise ValueError("request suite does not match evaluator")
        if request.budget_seconds <= 0:
            raise ValueError("evaluation budget must be positive")

    def _existing_report(self, request: EvaluationRequest) -> str | None:
        if not self.output_root.is_dir():
            return None
        for path in self.output_root.glob("*/report.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if document.get("request") == asdict(request):
                rebuild_evolution_report(path.parent)
                return path.parent.name
        return None


async def _run_trial(
    executor: TaskExecutor,
    arm: Arm,
    task: EvolutionSuiteTask,
    spec: TaskSpec,
    repeat: int,
    state_root: Path,
    report_root: Path,
    *,
    skill_name: str,
    skill_digest: str | None,
    invoke_skill: bool,
    budget_seconds: float,
) -> EvolutionTrial:
    """Run one graded trial in its own workspace against one installed state root."""
    trial_root = report_root / "workspaces" / arm / task.id / str(repeat)
    workspace = trial_root / "candidate"
    pristine = trial_root / "pristine"
    materialize_environment(spec, workspace)
    materialize_environment(spec, pristine)
    execution_spec = replace(
        spec,
        instruction=(
            f"/skill:{skill_name} {spec.instruction}" if invoke_skill else spec.instruction
        ),
    )
    started = time.perf_counter()
    metadata: Mapping[str, JSONValue] = {}
    error: str | None = None
    timed_out = False
    try:
        measured = await asyncio.wait_for(
            executor.execute(execution_spec, workspace, state_root),
            timeout=budget_seconds,
        )
        metadata = {**measured, "evaluated_skill_digest": skill_digest}
    except (ExecutionFailure, ExecutionCancelled, TimeoutError) as exc:
        result = getattr(exc, "result", None)
        metadata = result.metadata if result is not None else {}
        elapsed_ms = (time.perf_counter() - started) * 1000
        timed_out = _hit_own_budget(exc, elapsed_ms, budget_seconds)
        if not timed_out:
            error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    metadata = {**metadata, "evaluated_skill_digest": skill_digest}
    duration_ms = (time.perf_counter() - started) * 1000
    # The escape scan reads only this trial's own transcript; a missing session is
    # recorded as unavailable and treated as not escaped.
    escaped, evidence, scan = _scan_workspace_escape(workspace, metadata)
    metadata = {**metadata, "escape_scan": scan}
    if evidence is not None:
        metadata = {**metadata, "workspace_escape_targets": evidence}

    if error is not None or timed_out:
        # Hitting this trial's own watchdog is a failed trial, not broken
        # infrastructure: it stays in the denominator and leaves the gate clean.
        return EvolutionTrial(
            task.id,
            task.split,
            arm,
            repeat,
            False,
            error,
            duration_ms,
            metadata,
            timed_out=timed_out,
            workspace_escape=escaped,
        )
    try:
        proposition = await DualPropositionVerifier(GraderSuiteRunner(spec), repeats=1).verify(
            pristine, workspace, targets=spec.fail_to_pass
        )
    except Exception as exc:
        return EvolutionTrial(
            task.id,
            task.split,
            arm,
            repeat,
            False,
            f"grader: {type(exc).__name__}: {exc}",
            duration_ms,
            metadata,
            workspace_escape=escaped,
        )
    return EvolutionTrial(
        task.id,
        task.split,
        arm,
        repeat,
        proposition.succeeded and not escaped,
        None,
        duration_ms,
        metadata,
        fail_to_pass=tuple(sorted(proposition.fail_to_pass)),
        pass_to_pass=tuple(sorted(proposition.pass_to_pass)),
        unmet_targets=tuple(sorted(proposition.unmet_targets)),
        newly_failing=tuple(sorted(proposition.newly_failing)),
        flaky=tuple(sorted(proposition.flaky)),
        workspace_escape=escaped,
    )


def _hit_own_budget(exc: BaseException, elapsed_ms: float, budget_seconds: float) -> bool:
    """Whether a cancellation is this trial's own watchdog, not a cold interruption.

    ``asyncio.wait_for`` surfaces a watchdog hit either as ``TimeoutError`` or, when
    the executor converts the cancellation, as ``ExecutionCancelled``. Either form
    counts only when the elapsed time has reached this trial's own budget.
    """
    if not isinstance(exc, (ExecutionCancelled, TimeoutError)):
        return False
    budget_ms = budget_seconds * 1000
    tolerance_ms = max(
        _TRIAL_TIMEOUT_TOLERANCE_FLOOR_SECONDS * 1000,
        budget_ms * _TRIAL_TIMEOUT_TOLERANCE_RATIO,
    )
    return elapsed_ms >= budget_ms - tolerance_ms


def _scan_workspace_escape(
    workspace: Path, metadata: Mapping[str, JSONValue]
) -> tuple[bool, str | None, str]:
    """Scan one trial's own session for tool calls naming a target outside it.

    The transcript is the trial's own ``<workspace>/.run/sessions`` file, preferred by
    the executor's ``session_id`` and otherwise the newest non-dotfile session. The
    scan is offline and model-free. A session that cannot be read is reported as
    ``unavailable`` and treated as not escaped.
    """
    workspace = workspace.resolve()
    session = _session_file(workspace, metadata)
    if session is None:
        return False, None, _ESCAPE_SCAN_UNAVAILABLE
    try:
        lines = session.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, None, _ESCAPE_SCAN_UNAVAILABLE
    targets: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, Mapping) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "toolCall":
                continue
            for key, value in _path_arguments(item.get("arguments")):
                for token in _argument_tokens(value, single=key != "command"):
                    target = _escaping_target(workspace, token)
                    if target is not None:
                        targets.append(target)
    if not targets:
        return False, None, _ESCAPE_SCAN_OK
    return True, _escape_evidence(targets), _ESCAPE_SCAN_OK


def _session_file(workspace: Path, metadata: Mapping[str, JSONValue]) -> Path | None:
    """Resolve the trial's own session transcript, newest first as a fallback."""
    sessions = workspace / ".run" / "sessions"
    session_id = metadata.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        direct = sessions / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
    try:
        files = [
            item for item in sessions.iterdir() if item.is_file() and not item.name.startswith(".")
        ]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=_session_mtime)


def _session_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _path_arguments(node: object, key: str | None = None) -> Iterator[tuple[str, str]]:
    """Yield ``(argument key, string)`` for every path-bearing tool argument."""
    if isinstance(node, Mapping):
        for name, value in node.items():
            yield from _path_arguments(value, str(name).lower())
    elif isinstance(node, list):
        for value in node:
            yield from _path_arguments(value, key)
    elif isinstance(node, str) and key in _ESCAPE_ARGUMENT_KEYS:
        yield key, node


def _argument_tokens(value: str, *, single: bool) -> list[str]:
    """Split one argument value into candidate targets.

    A non-command value is also checked whole, so a path containing spaces stays
    visible; a command is only split into shell tokens.
    """
    tokens: list[str] = []
    if single and value.strip():
        tokens.append(value.strip())
    tokens.extend(part for part in _ESCAPE_TOKEN_SEPARATORS.split(value) if part)
    return tokens


def _path_like(token: str) -> bool:
    """Reject code and regex fragments that only start with a separator.

    A doubled backslash (``\\\\u`` in a Python literal, ``\\\\cache\\\\`` in a FindStr
    regex, ``\\\\candidate\\\\.run`` in a quoted expression) is never a literal Windows
    path, and a drive root holding exactly one escape-letter character (``e:\\n``) is
    a code fragment, not a target.
    """
    if token.startswith("\\\\"):
        body = token[2:]
        if "\\\\" in body or "\\" not in body:
            return False
        server, _, share = body.partition("\\")
        return bool(server) and bool(share)
    if len(token) >= 2 and token[1] == ":":
        rest = token[2:]
        return "\\\\" not in token and rest[:1] in ("\\", "/") and len(rest) > 2
    return True


def _escaping_target(workspace: Path, raw: str) -> str | None:
    """Return the raw token when it provably points outside ``workspace``.

    A token is a target only when the running platform calls it absolute (a drive or
    UNC path on Windows) or when it carries a ``..`` segment; anything else is a plain
    relative argument that cannot leave the trial. The target is resolved against the
    workspace, so traversal is caught, and every path inside the workspace - including
    ``<workspace>/.run/**`` sessions and blobs - is explicitly allowed.
    """
    token = raw.strip().strip("\"'`").rstrip(",;:")
    if not token or not _path_like(token):
        return None
    traversal = bool(_ESCAPE_TRAVERSAL_SEGMENT.search(token)) and ("\\" in token or "/" in token)
    if not traversal and not Path(token).is_absolute():
        return None
    try:
        resolved = (workspace / token).resolve()
    except (OSError, RuntimeError, ValueError):
        return token
    return None if resolved.is_relative_to(workspace) else token


def _escape_evidence(targets: Sequence[str]) -> str:
    """Cap the recorded evidence so one hostile transcript cannot bloat a trial row."""
    unique = list(dict.fromkeys(targets))
    rows = [
        target
        if len(target) <= _ESCAPE_TARGET_CHARS
        else target[: _ESCAPE_TARGET_CHARS - 3] + "..."
        for target in unique[:_ESCAPE_TARGET_LIMIT]
    ]
    text = "; ".join(rows)
    if len(unique) > _ESCAPE_TARGET_LIMIT:
        text += f"; (+{len(unique) - _ESCAPE_TARGET_LIMIT} more)"
    return text


@dataclass(frozen=True, slots=True)
class EvolutionArmRequest:
    """One frozen arm (optionally one ablation) outside the product gate path."""

    arm: EvolutionArm
    skill: str
    scope: MemoryScope = "user"
    ablation: AblationName | None = None
    candidate_id: str | None = None
    budget_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class EvolutionArmReport:
    """A written arm report plus the frozen document ``evolve-rebuild`` verifies."""

    report_id: str
    root: Path
    document: dict[str, Any]

    @property
    def arm(self) -> str:
        return str(self.document["arm"])

    @property
    def ablation(self) -> str | None:
        value = self.document.get("ablation")
        return str(value) if value is not None else None

    @property
    def label(self) -> str:
        return str(self.document["label"])

    @property
    def passed(self) -> bool:
        return bool(self.document["passed"])

    @property
    def summary(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.document["summary"])

    @property
    def source(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.document["source"])


class EvolutionArmRefused(ValueError):
    """An arm refused by admission, structure, fact or behavior policy."""


@dataclass(frozen=True, slots=True)
class _PreparedArm:
    """The content one arm measures, the checks behind it and its admission record."""

    content: str | None
    installed_digest: str | None
    candidate: SkillCandidate | None
    base_digest: str | None
    checks: Mapping[str, bool]
    behavior_gate_report: str | None
    facts_verified: bool
    # ``admitted`` names whether the measured state carries the candidate revision; a
    # fallback names the content installed instead of it, with the reason for that.
    admitted: bool = False
    fallback: str | None = None
    reason: str | None = None


_NO_CHECKS: Mapping[str, bool] = {"structure": False, "facts": False, "behavior_gate": False}
# The product state after a refused (or absent) paired gate: nothing is published, so the
# frozen formal SKILL.md keeps serving every session. That state is what gated-evolution
# then measures, so the arm lands on the same data as static-skill - which is the finding
# itself, not a defect of the arm. Only a missing candidate refuses the arm.
_FORMAL_SKILL_FALLBACK = "formal-skill"
_NO_GATE_PASSED_REPORT = "no gate-passed paired report"


class EvolutionArmEvaluationService:
    """Measure one arm, or one ablation of an arm, as offline-rebuildable evidence.

    ``gated-evolution`` measures the Skill the product path would really use: a verified,
    gate-passed paired report admits the candidate revision, while a refused or missing
    report falls back to the frozen formal ``SKILL.md`` and records ``admitted: false``
    with ``fallback: "formal-skill"``. ``ungated-revision`` is deliberately the
    non-product comparison arm and skips structure, fact and behavior checks entirely.
    The two ablations remove exactly one gate each, so a campaign can quantify that gate
    alone: ``-behavior-gate`` installs the candidate once structure and facts pass, and
    ``-project-probe`` refuses any candidate that cites project facts. Only a missing or
    unreadable candidate refuses an arm. Nothing here publishes a Skill or advances a
    candidate.
    """

    available = True

    def __init__(
        self,
        *,
        suite: Path,
        output_root: Path,
        candidates: SkillCandidateStore,
        skills: SkillManager,
        executor: TaskExecutor | None = None,
        repeats: int = 3,
        concurrency: int = 1,
        probe: ProjectProbe | None = None,
        report_roots: Sequence[Path] = (),
    ) -> None:
        if repeats < 1:
            raise ValueError("Evolution repeats must be positive")
        if concurrency < 1:
            raise ValueError("Evolution concurrency must be positive")
        self.suite = load_evolution_suite(suite)
        self.output_root = output_root.resolve()
        self.candidates = candidates
        self.skills = skills
        self.executor = executor or CodingExecutor()
        self.repeats = repeats
        self.concurrency = concurrency
        self.probe = probe
        self.report_roots = tuple(path.resolve() for path in report_roots)

    async def evaluate(self, request: EvolutionArmRequest) -> EvolutionArmReport:
        self._validate_request(request)
        prepared = self._prepare(request)
        label = _arm_label(request.arm, request.ablation)
        report_id = uuid4().hex
        root = self.output_root / label / report_id
        if root.exists():
            raise FileExistsError(root)
        root.mkdir(parents=True)
        trials = await self._measure(request, prepared, label, root)
        _write_json(root / "trials.json", [_trial_json(trial) for trial in trials])
        summary = reduce_evolution_trials(trials, repeats=self.repeats)
        source = self._source(request, prepared, label)
        report = {
            "schema": EVOLUTION_REPORT_SCHEMA,
            "kind": "arm",
            "report_id": report_id,
            "label": label,
            "arm": request.arm,
            "ablation": request.ablation,
            "non_product": request.arm == "ungated-revision",
            "measured_content_hash": prepared.installed_digest,
            "passed": _summary_passed(summary),
            "summary": summary,
            "source": source,
        }
        _write_json(root / "source.json", source)
        _write_json(root / "report.json", report)
        _write_json(
            root / "inventory.json",
            {
                "schema": EVOLUTION_REPORT_SCHEMA,
                "files": _inventory(root, exclude={"inventory.json"}),
            },
        )
        return EvolutionArmReport(report_id, root, report)

    def _validate_request(self, request: EvolutionArmRequest) -> None:
        if request.arm in CANDIDATE_ARMS:
            if request.candidate_id is None:
                raise EvolutionArmRefused(f"{request.arm} needs an explicit candidate id")
        elif request.candidate_id is not None:
            raise EvolutionArmRefused(
                f"{request.arm} does not install a candidate; drop the candidate id"
            )
        if request.ablation is not None and request.arm != "gated-evolution":
            raise EvolutionArmRefused(
                f"the {request.ablation} ablation applies only to gated-evolution"
            )
        if not request.skill.strip():
            raise EvolutionArmRefused("an evolution arm needs a Skill name")
        if request.budget_seconds <= 0:
            raise EvolutionArmRefused("evolution arm budget must be positive")

    def _prepare(self, request: EvolutionArmRequest) -> _PreparedArm:
        if request.arm == "no-skill":
            return _PreparedArm(None, None, None, None, dict(_NO_CHECKS), None, False)
        if request.arm == "static-skill":
            content = self.skills.main_content(request.scope, request.skill)
            if content is None:
                raise EvolutionArmRefused(
                    f"static-skill needs an installed Skill {request.skill!r} "
                    f"in the {request.scope} scope"
                )
            digest = _sha256(content.encode())
            return _PreparedArm(content, digest, None, digest, dict(_NO_CHECKS), None, False)

        candidate = self._require_candidate(request)
        content = self._candidate_content(candidate)
        digest = _sha256(content.encode())
        if digest != candidate.candidate_digest:
            raise EvolutionArmRefused("candidate content digest changed before evaluation")
        if request.arm == "ungated-revision":
            # The explicit non-product comparison arm: the revision is installed as-is,
            # so structure, fact and behavior policy never run.
            return _PreparedArm(
                content,
                digest,
                candidate,
                candidate.base_digest,
                dict(_NO_CHECKS),
                None,
                False,
                admitted=True,
            )

        self._check_structure(candidate, content)
        self._check_base_digest(candidate)
        # ``checks`` records which gates ran and were satisfied; a gate that was skipped
        # by an ablation and a gate that refused both leave their entry False, and the
        # top-level ``behavior_gate`` field of the source tells the two apart.
        checks = {"structure": True, "facts": True, "behavior_gate": False}
        facts_verified = False
        if request.ablation == "project-probe":
            # With project probing disabled no probe is ever verified, and any candidate
            # that cites project facts is refused outright instead of being checked.
            if candidate.claims:
                raise EvolutionArmRefused(
                    "project-probe ablation refuses a candidate that cites project facts: "
                    f"{len(candidate.claims)} claim(s) carry probe paths"
                )
        else:
            facts_verified = self._check_facts(candidate)
        behavior_report = (
            None if request.ablation == "behavior-gate" else self._passing_report(candidate)
        )
        if behavior_report is not None:
            checks["behavior_gate"] = True
        if behavior_report is None and request.ablation != "behavior-gate":
            # The paired gate refused the candidate, or no paired report exists at all:
            # the product path publishes nothing and keeps serving the frozen formal
            # Skill, so that is the Skill this arm must measure. A formal Skill that does
            # not exist is recorded as a null installed digest: the product state really
            # carries no Skill then.
            formal = self.skills.main_content(candidate.scope, candidate.name)
            return _PreparedArm(
                formal,
                _digest_optional(formal),
                candidate,
                candidate.base_digest,
                checks,
                None,
                facts_verified,
                admitted=False,
                fallback=_FORMAL_SKILL_FALLBACK,
                reason=_NO_GATE_PASSED_REPORT,
            )
        return _PreparedArm(
            content,
            digest,
            candidate,
            candidate.base_digest,
            checks,
            behavior_report,
            facts_verified,
            admitted=True,
        )

    def _require_candidate(self, request: EvolutionArmRequest) -> SkillCandidate:
        if request.candidate_id is None:
            raise EvolutionArmRefused(f"{request.arm} needs an explicit candidate id")
        try:
            candidate = self.candidates.require(request.candidate_id)
        except CandidateError as exc:
            raise EvolutionArmRefused(str(exc)) from exc
        if candidate.scope != request.scope or candidate.name != request.skill:
            raise EvolutionArmRefused(
                f"candidate {candidate.candidate_id} belongs to {candidate.scope}/{candidate.name}"
            )
        return candidate

    def _candidate_content(self, candidate: SkillCandidate) -> str:
        try:
            return self.candidates.content(candidate)
        except CandidateError as exc:
            raise EvolutionArmRefused(str(exc)) from exc

    def _check_structure(self, candidate: SkillCandidate, content: str) -> None:
        try:
            self.skills.validate_candidate(candidate.name, content)
        except SkillWriteError as exc:
            raise EvolutionArmRefused(f"structure check refused the candidate: {exc}") from exc

    def _check_base_digest(self, candidate: SkillCandidate) -> None:
        formal = self.skills.main_content(candidate.scope, candidate.name)
        if _digest_optional(formal) != candidate.base_digest:
            raise EvolutionArmRefused("formal Skill no longer matches the candidate baseline")

    def _check_facts(self, candidate: SkillCandidate) -> bool:
        for claim in candidate.claims:
            if not claim.probes:
                raise EvolutionArmRefused(
                    f"fact check refused a claim without a project probe: {claim.text!r}"
                )
        if self.probe is None:
            return False
        for claim in candidate.claims:
            for evidence in claim.probes:
                if not self.probe.verify(evidence):
                    raise EvolutionArmRefused(
                        f"fact check refused a drifted or unsafe project probe: {evidence.path!r}"
                    )
        return True

    def _passing_report(self, candidate: SkillCandidate) -> str | None:
        """Find the verified, gate-passed paired report of this exact candidate.

        A report counts only when it is a paired report (never another arm's), names the
        same candidate, carries the candidate digest as its measured content and still
        rebuilds from its own frozen evidence. ``None`` means the product gate refused or
        was never run, which is the state the arm then measures as the formal Skill.
        """
        for directory in (*self.report_roots, self.output_root):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*/report.json")):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(document, Mapping) or document.get("kind") == "arm":
                    continue
                request = document.get("request")
                if not isinstance(request, Mapping):
                    continue
                if request.get("candidate_id") != candidate.candidate_id:
                    continue
                if document.get("passed") is not True:
                    continue
                if document.get("measured_content_hash") != candidate.candidate_digest:
                    continue
                try:
                    rebuild_evolution_report(path.parent)
                except ValueError:
                    continue
                return path.parent.name
        return None

    async def _measure(
        self,
        request: EvolutionArmRequest,
        prepared: _PreparedArm,
        label: str,
        root: Path,
    ) -> list[EvolutionTrial]:
        async def run(task: EvolutionSuiteTask, spec: TaskSpec, repeat: int) -> EvolutionTrial:
            state_root = root / "state" / label / task.id / str(repeat)
            _install_skill(state_root, request.skill, prepared.content)
            measured = _installed_digest(state_root, request.skill)
            if measured != prepared.installed_digest:
                raise EvolutionArmRefused(f"{label}: installed Skill digest mismatch")
            return await _run_trial(
                self.executor,
                cast(Arm, label),
                task,
                spec,
                repeat,
                state_root,
                root,
                skill_name=request.skill,
                skill_digest=measured,
                invoke_skill=prepared.content is not None,
                budget_seconds=min(spec.budget_seconds, request.budget_seconds),
            )

        jobs = []
        for task in self.suite.tasks:
            if task.split == "train":
                continue
            spec = load_task_spec(task.path)
            for repeat in range(self.repeats):
                jobs.append(run(task, spec, repeat))
        trials: list[EvolutionTrial] = []
        for start in range(0, len(jobs), self.concurrency):
            trials.extend(await asyncio.gather(*jobs[start : start + self.concurrency]))
        return trials

    def _source(
        self, request: EvolutionArmRequest, prepared: _PreparedArm, label: str
    ) -> dict[str, Any]:
        candidate = prepared.candidate
        return {
            "arm": request.arm,
            "ablation": request.ablation,
            "label": label,
            "skill": request.skill,
            "scope": request.scope,
            "candidate_id": candidate.candidate_id if candidate is not None else None,
            "candidate_digest": candidate.candidate_digest if candidate is not None else None,
            "base_digest": prepared.base_digest,
            "installed_digest": prepared.installed_digest,
            "suite": str(self.suite.path),
            "suite_digest": self.suite.digest,
            "family": self.suite.family,
            "version": self.suite.version,
            "repeats": self.repeats,
            "concurrency": self.concurrency,
            "task_digests": {task.id: _directory_digest(task.path) for task in self.suite.tasks},
            "checks": dict(prepared.checks),
            "behavior_gate": _behavior_gate_state(request.arm, request.ablation),
            "facts_verified": prepared.facts_verified,
            "behavior_gate_report": prepared.behavior_gate_report,
            "admitted": prepared.admitted,
            "fallback": prepared.fallback,
            "reason": prepared.reason,
            "probe_root": str(self.probe.project_root) if self.probe is not None else None,
            "non_product": request.arm == "ungated-revision",
            "notes": _arm_notes(request.arm, request.ablation),
        }


def write_evolution_comparison(
    output_root: Path, reports: Sequence[EvolutionArmReport]
) -> dict[str, Any]:
    """Freeze a per-arm comparison view next to the arm reports it summarizes."""
    if not reports:
        raise ValueError("an evolution comparison needs at least one arm report")
    root = output_root.resolve()
    document = _comparison_document(root, [report.document for report in reports])
    _write_json(root / "comparison.json", document)
    (root / "REPORT.md").write_text(_comparison_markdown(document), encoding="utf-8")
    return document


def rebuild_evolution_comparison(root: Path) -> dict[str, Any]:
    """Verify a comparison view and every arm report it names, then return the view."""
    root = root.resolve()
    try:
        loaded = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"evolution comparison is missing: {root}") from exc
    if not isinstance(loaded, dict) or loaded.get("schema") != EVOLUTION_COMPARISON_SCHEMA:
        raise ValueError("Unsupported evolution comparison")
    documents: list[Mapping[str, Any]] = []
    for entry in loaded.get("arms") or []:
        if not isinstance(entry, Mapping):
            raise ValueError("Evolution comparison contains a malformed arm")
        label = str(entry.get("label") or "")
        report_id = str(entry.get("report_id") or "")
        relative = f"{label}/{report_id}"
        if not label or not report_id or entry.get("report") != relative:
            raise ValueError("Evolution comparison names a report it does not own")
        documents.append(rebuild_evolution_report(root / relative))
    expected = _comparison_document(root, documents)
    if expected != loaded:
        raise ValueError("Evolution comparison does not match the frozen arm reports")
    try:
        markdown = (root / "REPORT.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"evolution comparison report is missing: {root}") from exc
    if markdown != _comparison_markdown(expected):
        raise ValueError("Evolution comparison report does not match the frozen arm reports")
    return expected


def _comparison_document(
    output_root: Path, documents: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": EVOLUTION_COMPARISON_SCHEMA,
        "output_root": str(output_root),
        "scope_statement": EVOLUTION_SCOPE_STATEMENT,
        "denominator_rule": EVOLUTION_DENOMINATOR_RULE,
        "gate_rule": EVOLUTION_GATE_RULE,
        "arms": [_comparison_arm(document) for document in documents],
    }


def _comparison_arm(document: Mapping[str, Any]) -> dict[str, Any]:
    label = str(document["label"])
    report_id = str(document["report_id"])
    summary = cast(Mapping[str, Any], document["summary"])
    source = cast(Mapping[str, Any], document["source"])
    tasks = cast(Mapping[str, Mapping[str, Any]], summary["tasks"])
    rows: list[dict[str, Any]] = []
    for task_id, task in sorted(tasks.items()):
        measured = cast(Mapping[str, Any], task[label])
        passes = int(measured["passes"])
        trials = int(measured["trials"])
        rows.append(
            {
                "task_id": task_id,
                "split": str(task["split"]),
                "passes": passes,
                "trials": trials,
                "errors": int(measured["errors"]),
                "failed": trials - passes,
                "passed": bool(measured["passed"]),
            }
        )
    efficiency = cast(Mapping[str, Any], summary["efficiency"])
    return {
        "arm": str(document["arm"]),
        "ablation": document.get("ablation"),
        "label": label,
        "report": f"{label}/{report_id}",
        "report_id": report_id,
        "passed": bool(document["passed"]),
        "non_product": bool(document.get("non_product")),
        "admitted": bool(source.get("admitted")),
        "fallback": source.get("fallback"),
        "reason": source.get("reason"),
        "behavior_gate": source.get("behavior_gate"),
        "checks": source.get("checks"),
        "notes": source.get("notes"),
        "totals": {
            "tasks": len(rows),
            "tasks_passed": sum(1 for row in rows if row["passed"]),
            "trials": sum(int(row["trials"]) for row in rows),
            "passes": sum(int(row["passes"]) for row in rows),
            "errors": sum(int(row["errors"]) for row in rows),
            "failed": sum(int(row["failed"]) for row in rows),
        },
        "tasks": rows,
        "efficiency": efficiency.get(label),
    }


def _comparison_markdown(document: Mapping[str, Any]) -> str:
    lines = [
        "# Skill evolution arm comparison",
        "",
        f"- scope: {document['scope_statement']}",
        f"- denominator: {document['denominator_rule']}",
        f"- gate: {document['gate_rule']}",
        "",
    ]
    for arm in cast(list[Mapping[str, Any]], document["arms"]):
        lines.append(f"## {arm['label']}")
        lines.append("")
        lines.append(
            f"- report: `{arm['report']}`; campaign clean: {arm['passed']}; "
            f"non-product: {arm['non_product']}"
        )
        lines.append(
            f"- admitted: {arm['admitted']}; fallback: {arm['fallback']}; "
            f"reason: {arm['reason']}; behavior gate: {arm['behavior_gate']}"
        )
        for note in cast(list[str], arm["notes"]):
            lines.append(f"- note: {note}")
        lines.append("")
        lines.append("| split | task | passes | trials | errors | failed | passed |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in cast(list[Mapping[str, Any]], arm["tasks"]):
            lines.append(
                f"| {row['split']} | {row['task_id']} | {row['passes']} | {row['trials']} | "
                f"{row['errors']} | {row['failed']} | {row['passed']} |"
            )
        totals = cast(Mapping[str, Any], arm["totals"])
        efficiency = cast(Mapping[str, Any], arm["efficiency"] or {})
        lines.append("")
        lines.append(
            f"totals: tasks {totals['tasks_passed']}/{totals['tasks']}; "
            f"trials {totals['trials']}; passes {totals['passes']}; "
            f"errors {totals['errors']}; failed {totals['failed']}"
        )
        lines.append(
            f"efficiency: calls {efficiency.get('calls')}; "
            f"input tokens {efficiency.get('input_tokens')}; "
            f"output tokens {efficiency.get('output_tokens')}; "
            f"known cost {efficiency.get('known_cost')}; "
            f"total cost {efficiency.get('total_cost')}"
        )
        lines.append("")
    return "\n".join(lines) + "\n"


def _arm_label(arm: EvolutionArm, ablation: AblationName | None) -> str:
    return arm if ablation is None else f"{arm}-{ablation}"


def _behavior_gate_state(arm: EvolutionArm, ablation: AblationName | None) -> str | None:
    """Whether the paired behavior gate was applied to this arm's measurement."""
    if arm != "gated-evolution":
        return None
    return "skipped" if ablation == "behavior-gate" else "enforced"


def _arm_notes(arm: EvolutionArm, ablation: AblationName | None) -> list[str]:
    if arm == "no-skill":
        notes = ["no-skill: no Skill is installed or invoked"]
    elif arm == "static-skill":
        notes = ["static-skill: the frozen formal SKILL.md is installed and invoked unchanged"]
    elif arm == "ungated-revision":
        notes = [
            "ungated-revision: non-product comparison arm; the candidate is installed "
            "without structure, fact or behavior checks"
        ]
    else:
        notes = [
            "gated-evolution: product path; the revision is installed only after the "
            "structure, fact and paired behavior gates pass",
            "a refused or missing paired report falls back to the frozen formal SKILL.md, "
            "so this arm then matches static-skill by construction: the product behaviour "
            "does not change when the gate refuses",
        ]
    if ablation == "project-probe":
        notes.append("project-probe ablation: a candidate citing project facts is refused")
    elif ablation == "behavior-gate":
        notes.append(
            "behavior-gate ablation: structure and fact checks ran; the paired behavior "
            "gate was skipped"
        )
    return notes


def load_evolution_suite(path: Path) -> EvolutionSuite:
    path = path.resolve()
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode("utf-8"))
    if document.get("schema") != "run-agent.evolution-suite":
        raise ValueError("Unsupported evolution suite schema")
    tasks: list[EvolutionSuiteTask] = []
    for item in document.get("tasks", []):
        split = item.get("split")
        if split not in {"train", "selection", "test"}:
            raise ValueError(f"Invalid evolution split: {split}")
        task_path = (path.parent / str(item["path"])).resolve()
        tasks.append(EvolutionSuiteTask(str(item["id"]), split, str(item["topic"]), task_path))
    if not tasks or any(
        sum(task.split == split for task in tasks) != 3 for split in ("train", "selection", "test")
    ):
        raise ValueError("Evolution suite must contain exactly three tasks per split")
    return EvolutionSuite(
        path,
        str(document.get("version") or ""),
        str(document.get("family") or ""),
        tuple(tasks),
        _sha256(raw),
    )


def reduce_evolution_trials(trials: list[EvolutionTrial], *, repeats: int) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[EvolutionTrial]] = {}
    for trial in trials:
        grouped.setdefault((trial.split, trial.task_id, trial.arm), []).append(trial)
    tasks: dict[str, dict[str, Any]] = {}
    infra_errors = 0
    workspace_escapes = 0
    required_passes = repeats // 2 + 1
    for (split, task_id, arm), rows in sorted(grouped.items()):
        passes = sum(row.succeeded for row in rows)
        # A trial that hit its own watchdog is a failure in the denominator, never
        # an infrastructure error; a cold cancellation keeps its error.
        errors = sum(row.infrastructure_error is not None and not row.timed_out for row in rows)
        escapes = sum(row.workspace_escape for row in rows)
        infra_errors += errors
        workspace_escapes += escapes
        task = tasks.setdefault(task_id, {"split": split})
        measured: dict[str, Any] = {
            "passes": passes,
            "trials": len(rows),
            "errors": errors,
            "passed": errors == 0 and passes >= required_passes,
        }
        # Escapes are a count of their own, never an infrastructure error. The key is
        # written only when a trial escaped, so reports frozen before this field
        # existed still rebuild byte-for-byte with exactly the keys they carry.
        if escapes:
            measured["escapes"] = escapes
        task[arm] = measured

    selection_gate, measurement = _selection_outcome(tasks, infra_errors, repeats, bool(trials))
    summary: dict[str, Any] = {"tasks": tasks, "selection_gate": selection_gate}
    if measurement is not None:
        summary["measurement"] = measurement
    if workspace_escapes:
        summary["workspace_escapes"] = workspace_escapes
    summary["efficiency"] = _efficiency(trials)
    return summary


def _selection_outcome(
    tasks: Mapping[str, Mapping[str, Any]], infra_errors: int, repeats: int, measured: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Compute the paired gate when both paired arms exist, else an arm measurement.

    The product path always supplies ``baseline`` and ``candidate`` trials and keeps
    exactly the gate it always had. Any other arm set gets a per-arm measurement
    instead, so a campaign can reduce arbitrary arms without forking the reducer.
    """
    selection = [row for row in tasks.values() if row["split"] == "selection"]
    if selection and all("baseline" in row and "candidate" in row for row in selection):
        regressions: list[str] = []
        improvements: list[str] = []
        for task_id, row in tasks.items():
            if row["split"] != "selection":
                continue
            baseline = bool(row["baseline"]["passed"])
            candidate = bool(row["candidate"]["passed"])
            if baseline and not candidate:
                regressions.append(task_id)
            if not baseline and candidate:
                improvements.append(task_id)
        required = repeats // 2 + 1
        gate = {
            "passed": infra_errors == 0 and not regressions and bool(improvements),
            "infrastructure_errors": infra_errors,
            "regressions": regressions,
            "improvements": improvements,
            "rule": (
                f"{required}-of-{repeats}; no selection regression and at least one improvement"
            ),
        }
        return gate, None
    gate = {
        "applicable": False,
        "reason": "the paired selection gate needs both the baseline and the candidate arm",
        "infrastructure_errors": infra_errors,
    }
    measurement = {
        "passed": infra_errors == 0 and measured,
        "infrastructure_errors": infra_errors,
        "rule": (
            "arm campaign: failures, timeouts and empty patches stay in the denominator; "
            "infrastructure errors fail the campaign"
        ),
    }
    return gate, measurement


def _summary_passed(summary: Mapping[str, Any]) -> bool:
    """Read the frozen pass flag of either a paired report or an arm report."""
    gate = summary.get("selection_gate")
    if isinstance(gate, Mapping) and "passed" in gate:
        return bool(gate["passed"])
    measurement = summary.get("measurement")
    return isinstance(measurement, Mapping) and bool(measurement.get("passed"))


def _efficiency(trials: list[EvolutionTrial]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in sorted({trial.arm for trial in trials}):
        rows = [trial for trial in trials if trial.arm == arm]
        costs = [trial.metadata.get("cost") for trial in rows]
        complete = bool(rows) and all(isinstance(value, int | float) for value in costs)
        summary[arm] = {
            "trials": len(rows),
            "calls": sum(_number(row.metadata.get("calls")) for row in rows),
            "input_tokens": sum(_number(row.metadata.get("input_tokens")) for row in rows),
            "output_tokens": sum(_number(row.metadata.get("output_tokens")) for row in rows),
            "cache_read_tokens": sum(
                _number(row.metadata.get("cache_read_tokens")) for row in rows
            ),
            "cache_write_tokens": sum(
                _number(row.metadata.get("cache_write_tokens")) for row in rows
            ),
            "known_cost": sum(
                float(value)
                for row in rows
                if isinstance((value := row.metadata.get("known_cost")), int | float)
            ),
            "total_cost": sum(_float_cost(value) for value in costs) if complete else None,
        }
    return summary


def _number(value: JSONValue) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _float_cost(value: JSONValue) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def rebuild_evolution_report(root: Path) -> dict[str, Any]:
    root = root.resolve()
    inventory_path = root / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory.get("schema") != EVOLUTION_REPORT_SCHEMA:
        raise ValueError("Unsupported evolution inventory")
    for item in inventory.get("files", []):
        path = root / str(item["path"])
        if not path.is_file() or path.stat().st_size != int(item["size"]):
            raise ValueError(f"Evolution evidence missing or wrong size: {item['path']}")
        if _file_digest(path) != item["sha256"]:
            raise ValueError(f"Evolution evidence hash mismatch: {item['path']}")
    report = cast(dict[str, Any], json.loads((root / "report.json").read_text(encoding="utf-8")))
    if report.get("schema") != EVOLUTION_REPORT_SCHEMA:
        raise ValueError("Unsupported evolution report")
    source = cast(dict[str, Any], json.loads((root / "source.json").read_text(encoding="utf-8")))
    if source != report.get("source"):
        raise ValueError("Evolution source does not match report")
    trials = [
        _trial_from_json(item)
        for item in json.loads((root / "trials.json").read_text(encoding="utf-8"))
    ]
    rebuilt = reduce_evolution_trials(trials, repeats=int(source["repeats"]))
    if rebuilt != report.get("summary"):
        raise ValueError("Evolution report does not match frozen trials")
    if bool(report.get("passed")) != _summary_passed(rebuilt):
        raise ValueError("Evolution report pass flag does not match frozen trials")
    return report


def _trial_json(trial: EvolutionTrial) -> dict[str, Any]:
    return {
        **asdict(trial),
        "metadata": dict(trial.metadata),
    }


def _trial_from_json(raw: Mapping[str, Any]) -> EvolutionTrial:
    return EvolutionTrial(
        task_id=str(raw["task_id"]),
        split=raw["split"],
        arm=raw["arm"],
        repeat=int(raw["repeat"]),
        succeeded=bool(raw["succeeded"]),
        infrastructure_error=raw.get("infrastructure_error"),
        duration_ms=float(raw["duration_ms"]),
        metadata=cast(Mapping[str, JSONValue], raw.get("metadata", {})),
        timed_out=bool(raw.get("timed_out", False)),
        fail_to_pass=tuple(raw.get("fail_to_pass", ())),
        pass_to_pass=tuple(raw.get("pass_to_pass", ())),
        unmet_targets=tuple(raw.get("unmet_targets", ())),
        newly_failing=tuple(raw.get("newly_failing", ())),
        flaky=tuple(raw.get("flaky", ())),
        workspace_escape=bool(raw.get("workspace_escape", False)),
    )


def _install_skill(state_root: Path, name: str, content: str | None) -> None:
    if state_root.exists():
        shutil.rmtree(state_root)
    state_root.mkdir(parents=True)
    if content is None:
        return
    target = state_root / "skills" / name / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(content.encode("utf-8"))


def _installed_digest(state_root: Path, name: str) -> str | None:
    path = state_root / "skills" / name / "SKILL.md"
    return _sha256(path.read_bytes()) if path.is_file() else None


def _digest_optional(content: str | None) -> str | None:
    return _sha256(content.encode()) if content is not None else None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _inventory(root: Path, *, exclude: set[str]) -> list[dict[str, JSONValue]]:
    rows: list[dict[str, JSONValue]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if (
            relative in exclude
            or relative.startswith("workspaces/")
            or relative.startswith("state/")
        ):
            continue
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": _file_digest(path)})
    return rows


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "CANDIDATE_ARMS",
    "EVOLUTION_ABLATIONS",
    "EVOLUTION_ARMS",
    "EVOLUTION_COMPARISON_SCHEMA",
    "EVOLUTION_DENOMINATOR_RULE",
    "EVOLUTION_GATE_RULE",
    "EVOLUTION_REPORT_SCHEMA",
    "EVOLUTION_SCOPE_STATEMENT",
    "AblationName",
    "Arm",
    "CodingExecutor",
    "EvolutionArm",
    "EvolutionArmEvaluationService",
    "EvolutionArmRefused",
    "EvolutionArmReport",
    "EvolutionArmRequest",
    "EvolutionEvaluationService",
    "EvolutionSuite",
    "EvolutionSuiteTask",
    "EvolutionTrial",
    "TaskExecutor",
    "load_evolution_suite",
    "rebuild_evolution_comparison",
    "rebuild_evolution_report",
    "reduce_evolution_trials",
    "write_evolution_comparison",
]
