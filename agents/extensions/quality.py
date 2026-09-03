"""Verification, bounded correction, and acceptance extensions."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from ..correction import CorrectionController
from ..execution import SandboxError
from ..execution.workspace import git_diff
from ..harness.budget import BudgetExceeded
from ..harness.failures import FailureInfo, FailureKind
from ..harness.task import CorrectionAttempt, PatchCandidate, TaskPhase, TaskSpec, TaskState
from ..session import OperationType, SessionRepository
from ..verification import VerificationOrchestrator, VerificationReport
from ..verification.discovery import VerificationCommand
from .contracts import ExtensionAPI, ExtensionContext, ExtensionEvent


def _sha(value: str) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def build_verification_commands(
    task: TaskSpec, raw: Any
) -> tuple[VerificationCommand, ...] | None:
    if raw is None:
        return None
    return tuple(
        item
        if isinstance(item, VerificationCommand)
        else VerificationCommand(
            f"verification-{index + 1}",
            tuple(str(value) for value in item),
            task.runtime.verification.timeout_seconds,
            focused=True,
        )
        for index, item in enumerate(raw)
    )


def verify_task(
    task: TaskSpec, state: TaskState, patch: str
) -> Any:
    commands = build_verification_commands(
        task, task.runtime.verification.commands
    )
    orchestrator = VerificationOrchestrator(
        workspace_root=state.workspace,
        commands=commands,
        sandbox=getattr(state.execution, "session", None),
        profile=task.verification_profile,
        require_patch=task.mode in {"coding", "swebench"},
        require_focused_tests=task.mode in {"coding", "swebench"},
    )
    return orchestrator.verify(state.journal.observe(), patch=patch)


def assess_task(task: TaskSpec, state: TaskState) -> Any:
    commands = build_verification_commands(
        task, task.runtime.verification.acceptance_commands
    ) or ()
    orchestrator = VerificationOrchestrator(
        workspace_root=state.workspace,
        commands=commands,
        sandbox=getattr(state.execution, "session", None),
        profile=task.verification_profile,
        require_patch=False,
        require_focused_tests=True,
    )
    return orchestrator.verify(state.journal.observe())


def repair_prompt(report: VerificationReport, *, repeated: bool) -> str:
    hint = (
        "\nThe failure fingerprint repeated. Change the localization strategy instead of repeating the prior edit."
        if repeated
        else ""
    )
    return (
        "You are in a bounded correction round. Use the verifier evidence below as the source of truth.\n\n"
        + report.feedback()
        + hint
        + "\nMake the smallest correction, run only focused checks through the available tools, and finish within two turns."
    )


def restore_candidate(
    task: TaskSpec,
    state: TaskState,
    candidate: PatchCandidate,
    *,
    artifact_root: Path | None = None,
) -> bool:
    if not task.runtime.verification.disposable_workspace or not candidate.patch_path:
        return False
    root = state.workspace.resolve()
    try:
        root.relative_to(Path(tempfile.gettempdir()).resolve())
    except ValueError:
        return False
    path = Path(candidate.patch_path).resolve()
    if artifact_root is not None:
        expected_parent = (artifact_root / "patches").resolve()
        if path.parent != expected_parent:
            return False
    if not (root / ".git").is_dir() or not path.is_file() or not state.changes.base_commit:
        return False
    patch = path.read_text(encoding="utf-8")
    if _sha(patch) != candidate.patch_sha256:
        return False
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
        check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != state.changes.base_commit:
        return False
    with tempfile.TemporaryDirectory(prefix="run-agent-index-") as temporary:
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        prepared = subprocess.run(
            ["git", "read-tree", state.changes.base_commit],
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
            check=False,
        )
        checked = (
            subprocess.run(
                ["git", "apply", "--check", "--cached", "--whitespace=nowarn", "-"],
                cwd=str(root),
                env=environment,
                input=patch,
                capture_output=True,
                text=True,
                shell=False,
                timeout=60,
                check=False,
            )
            if prepared.returncode == 0
            else prepared
        )
    if checked.returncode != 0:
        return False
    previous = git_diff(root)
    for argv in (
        ["git", "reset", "--hard", state.changes.base_commit],
        ["git", "clean", "-fd", "-e", ".run/"],
    ):
        if subprocess.run(
            argv,
            cwd=str(root),
            capture_output=True,
            shell=False,
            timeout=60,
            check=False,
        ).returncode != 0:
            return False
    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=str(root),
        input=patch,
        capture_output=True,
        text=True,
        shell=False,
        timeout=60,
        check=False,
    )
    if applied.returncode == 0:
        return True
    subprocess.run(
        ["git", "reset", "--hard", state.changes.base_commit],
        cwd=str(root),
        capture_output=True,
        shell=False,
        timeout=60,
        check=False,
    )
    subprocess.run(
        ["git", "clean", "-fd", "-e", ".run/"],
        cwd=str(root),
        capture_output=True,
        shell=False,
        timeout=60,
        check=False,
    )
    if previous:
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(root),
            input=previous,
            capture_output=True,
            text=True,
            shell=False,
            timeout=60,
            check=False,
        )
    return False


def save_candidate(
    state: TaskState,
    repository: SessionRepository,
    artifact_root: Path,
    patch: str,
    report: VerificationReport,
) -> PatchCandidate:
    path = artifact_root / "patches" / f"candidate-{len(state.candidates) + 1:02d}.diff"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(patch, encoding="utf-8")
    passed = sum(item.passed for item in report.steps)
    failed = sum(not item.passed for item in report.steps)
    gates = {"workspace-boundary", "patch-applies-to-base", "python-syntax", "diff-check"}
    candidate = PatchCandidate(
        f"candidate-{len(state.candidates) + 1:02d}",
        _sha(patch) or "",
        tuple(sorted(state.journal.changed_paths)),
        report.outcome,
        passed,
        failed,
        state.budgets.turns_used,
        str(path),
        bool(patch.strip())
        and all(item.passed for item in report.steps if item.name in gates),
        len(patch.encode("utf-8")),
    )
    state.candidates.append(candidate)
    state.candidate_reports[candidate.candidate_id] = report
    repository.add_artifact(
        state.session_id,
        kind="patch_candidate",
        path=str(path),
        sha256=candidate.patch_sha256,
        run_id=state.run_id,
        metadata=candidate.__dict__,
    )
    return candidate


def best_candidate(state: TaskState) -> PatchCandidate | None:
    ranks = {"PASS": 3, "INCONCLUSIVE": 2, "FAIL": 1, "INFRASTRUCTURE_FAILURE": 0}
    return (
        max(
            state.candidates,
            key=lambda item: (
                int(item.required_gates_passed),
                ranks.get(item.outcome, 0),
                item.passed_steps,
                -item.failed_steps,
                -item.patch_bytes,
            ),
        )
        if state.candidates
        else None
    )


def _append_operation(
    context: ExtensionContext, operation: OperationType, payload: dict[str, Any]
) -> None:
    context.repository.append_operation(
        context.state.session_id,
        context.state.lane_id,
        operation,
        payload,
        run_id=context.state.run_id,
    )


def _observe_workspace(context: ExtensionContext) -> None:
    changed = context.state.journal.observe()
    context.state.changes.update(changed, context.state.journal.content_hashes())


def setup_verification(api: ExtensionAPI) -> None:
    async def after_solve(_event: ExtensionEvent, context: ExtensionContext) -> None:
        if (
            context.task.mode == "interactive"
            and context.task.runtime.verification.commands is None
            and not context.state.journal.observe()
        ):
            report = VerificationReport(
                True,
                (),
                (),
                skipped_reason="no workspace changes",
                outcome="PASS",
            )
            context.outcome.report = report
            context.state.verification_history.append(report)
            return
        context.state.phase = TaskPhase.VERIFYING
        _append_operation(
            context,
            OperationType.VERIFICATION_STARTED,
            {"patch_sha256": _sha(context.outcome.patch)},
        )
        report = await verify_task(context.task, context.state, context.outcome.patch)
        context.outcome.report = report
        context.state.verification_history.append(report)
        if report.outcome == "INFRASTRUCTURE_FAILURE":
            context.outcome.failure = FailureInfo(
                FailureKind.INFRASTRUCTURE,
                report.skipped_reason or "verification infrastructure failure",
            )
        _append_operation(context, OperationType.VERIFICATION_FINISHED, report.to_dict())
        save_candidate(
            context.state,
            context.repository,
            context.artifact_root,
            context.outcome.patch,
            report,
        )

    api.on("after_solve", after_solve)


def setup_correction(api: ExtensionAPI) -> None:
    controller = CorrectionController()

    async def after_solve(_event: ExtensionEvent, context: ExtensionContext) -> None:
        report = context.outcome.report
        if report is None:
            raise RuntimeError("correction requires a verification report")
        while report.outcome not in {"PASS", "INFRASTRUCTURE_FAILURE"}:
            try:
                attempt = context.state.budgets.begin_repair_attempt()
            except BudgetExceeded as exc:
                context.outcome.failure = FailureInfo(FailureKind.BUDGET, str(exc))
                break
            best = best_candidate(context.state)
            decision = controller.decide(
                report,
                has_restorable_candidate=bool(
                    best and context.task.runtime.verification.disposable_workspace
                ),
            )
            if decision.action == "restore_best_and_change_strategy" and best:
                if restore_candidate(
                    context.task,
                    context.state,
                    best,
                    artifact_root=context.artifact_root,
                ):
                    _observe_workspace(context)
            context.state.phase = TaskPhase.CORRECTING
            before_hash = _sha(context.outcome.patch)
            _append_operation(
                context,
                OperationType.CORRECTION_STARTED,
                {"attempt": attempt, "fingerprint": report.fingerprint, "action": decision.action},
            )
            try:
                run_agent = context.require("run_agent")
                consume_output = context.require("consume_output")
                export_patch = context.require("export_patch")
                output = await run_agent(
                    repair_prompt(report, repeated=decision.repeated),
                    min(2, context.state.budgets.repair_remaining),
                )
                context.outcome.final_text = str(
                    output.get("text") or context.outcome.final_text
                )
                consume_output(output, "repair")
                _observe_workspace(context)
                context.outcome.patch = await export_patch()
                context.state.phase = TaskPhase.VERIFYING
                _append_operation(
                    context,
                    OperationType.VERIFICATION_STARTED,
                    {"attempt": attempt, "patch_sha256": _sha(context.outcome.patch)},
                )
                report = await verify_task(
                    context.task, context.state, context.outcome.patch
                )
                context.outcome.report = report
                context.state.verification_history.append(report)
                after_hash = _sha(context.outcome.patch)
                previous = (
                    context.state.verification_history[-2].fingerprint
                    if len(context.state.verification_history) > 1
                    else None
                )
                context.state.correction_history.append(
                    CorrectionAttempt(
                        attempt,
                        decision.action,
                        decision.reason,
                        before_hash,
                        after_hash,
                        previous,
                    )
                )
                save_candidate(
                    context.state,
                    context.repository,
                    context.artifact_root,
                    context.outcome.patch,
                    report,
                )
                _append_operation(
                    context,
                    OperationType.CORRECTION_FINISHED,
                    {"attempt": attempt, "outcome": report.outcome, "patch_sha256": after_hash},
                )
            except BudgetExceeded as exc:
                try:
                    _observe_workspace(context)
                    context.outcome.patch = await context.require("export_patch")()
                except Exception:
                    pass
                context.outcome.failure = FailureInfo(FailureKind.BUDGET, str(exc))
                break
            except SandboxError as exc:
                context.outcome.failure = FailureInfo(FailureKind.INFRASTRUCTURE, str(exc))
                break
            except Exception as exc:
                context.outcome.failure = FailureInfo(
                    FailureKind.CORRECTION, f"{type(exc).__name__}: {exc}"
                )
                break

        if context.outcome.report is not None and context.outcome.report.outcome != "PASS":
            best = best_candidate(context.state)
            if best and best.patch_path and Path(best.patch_path).is_file():
                selected = best.patch_sha256 == _sha(context.outcome.patch)
                if not selected:
                    selected = restore_candidate(
                        context.task,
                        context.state,
                        best,
                        artifact_root=context.artifact_root,
                    )
                    if selected:
                        _observe_workspace(context)
                if selected:
                    context.outcome.patch = Path(best.patch_path).read_text(
                        encoding="utf-8"
                    )
                    selected_report = context.state.candidate_reports.get(
                        best.candidate_id
                    )
                    if selected_report is not None:
                        context.outcome.report = selected_report
            if context.outcome.report.outcome == "PASS":
                if context.outcome.failure and context.outcome.failure.kind in {
                    FailureKind.VERIFICATION,
                    FailureKind.CORRECTION,
                }:
                    context.outcome.failure = None
            elif context.outcome.report.outcome == "INFRASTRUCTURE_FAILURE":
                context.outcome.failure = context.outcome.failure or FailureInfo(
                    FailureKind.INFRASTRUCTURE,
                    context.outcome.report.skipped_reason
                    or "verification infrastructure failure",
                )
            else:
                context.outcome.failure = context.outcome.failure or FailureInfo(
                    FailureKind.VERIFICATION,
                    context.outcome.report.skipped_reason or "verification did not pass",
                    retryable=True,
                )

    api.on("after_solve", after_solve)


def setup_acceptance(api: ExtensionAPI) -> None:
    async def after_run(_event: ExtensionEvent, context: ExtensionContext) -> None:
        if context.task.runtime.verification.acceptance_commands is None:
            return
        failure = context.outcome.failure
        if failure is not None and failure.kind == FailureKind.INFRASTRUCTURE:
            return
        report = await assess_task(context.task, context.state)
        context.outcome.acceptance = report
        if report.outcome == "INFRASTRUCTURE_FAILURE":
            context.outcome.failure = FailureInfo(
                FailureKind.INFRASTRUCTURE,
                report.skipped_reason or "acceptance verification infrastructure failure",
            )
        elif report.outcome != "PASS":
            context.outcome.failure = FailureInfo(
                FailureKind.VERIFICATION,
                report.skipped_reason or "acceptance verification did not pass",
                retryable=True,
                details={"acceptance": report.to_dict()},
            )

    api.on("after_run", after_run)


__all__ = [
    "assess_task",
    "best_candidate",
    "build_verification_commands",
    "repair_prompt",
    "restore_candidate",
    "save_candidate",
    "setup_acceptance",
    "setup_correction",
    "setup_verification",
    "verify_task",
]
