"""Execute staged verification outside the model's self-assessment."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Sequence

from ..execution import ExecRequest, SandboxSession
from ..runtime.scope import current_workspace
from .discovery import VerificationCommand, discover_verification_commands
from .models import VerificationReport, VerificationStepResult


def _trim(value: str, limit: int = 12_000) -> str:
    if len(value) <= limit:
        return value
    half = limit // 2
    return value[:half] + f"\n[... {len(value) - limit} chars omitted ...]\n" + value[-half:]


class VerificationOrchestrator:
    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        commands: Sequence[VerificationCommand] | None = None,
        sandbox: SandboxSession | None = None,
        profile: str = "default",
        require_patch: bool = False,
        require_focused_tests: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root or current_workspace()).resolve()
        self.commands = tuple(commands) if commands is not None else None
        self.sandbox = sandbox
        self.profile = profile
        self.require_patch = bool(require_patch)
        self.require_focused_tests = bool(require_focused_tests)

    def _run_step(self, command: VerificationCommand) -> VerificationStepResult:
        started = time.perf_counter()
        try:
            result = subprocess.run(
                list(command.argv),
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_seconds,
                shell=False,
            )
            return VerificationStepResult(
                name=command.name,
                command=list(command.argv),
                passed=result.returncode == 0,
                exit_code=result.returncode,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                stdout=_trim(result.stdout or ""),
                stderr=_trim(result.stderr or ""),
            )
        except subprocess.TimeoutExpired as exc:
            return VerificationStepResult(
                name=command.name,
                command=list(command.argv),
                passed=False,
                exit_code=None,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                stdout=_trim(str(exc.stdout or "")),
                stderr=_trim(str(exc.stderr or "")),
                error=f"timed out after {command.timeout_seconds:.0f}s",
            )
        except Exception as exc:
            return VerificationStepResult(
                name=command.name,
                command=list(command.argv),
                passed=False,
                exit_code=None,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _sandbox_argv(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Translate host-specific interpreter paths to the image's tools."""
        values = tuple(str(item) for item in argv)
        if values and (values[0] == sys.executable or Path(values[0]).name.lower().startswith("python")):
            executable = self.sandbox.spec.python_executable if self.sandbox is not None else "python"
            return (executable, *values[1:])
        if values and Path(values[0]).name.lower() in {"npm", "npm.cmd", "npm.ps1"}:
            return ("npm", *values[1:])
        return values

    def _map_workspace_paths(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Map host checkout paths to the active container checkout mount."""
        mapped: list[str] = []
        root = self.workspace_root.resolve()
        container_root = self.sandbox.spec.container_workspace if self.sandbox is not None else "/workspace"
        for value in argv:
            candidate = Path(value)
            if candidate.is_absolute():
                try:
                    relative = candidate.resolve().relative_to(root)
                except (ValueError, OSError):
                    pass
                else:
                    mapped.append(container_root.rstrip("/") + "/" + relative.as_posix())
                    continue
            mapped.append(value)
        return tuple(mapped)

    async def _run_step_in_sandbox(self, command: VerificationCommand) -> VerificationStepResult:
        started = time.perf_counter()
        argv = self._map_workspace_paths(self._sandbox_argv(command.argv))
        container_root = self.sandbox.spec.container_workspace
        try:
            result = await self.sandbox.exec(
                ExecRequest(argv=argv, cwd=container_root, timeout_seconds=command.timeout_seconds)
            )
            return VerificationStepResult(
                name=command.name,
                command=list(argv),
                passed=result.ok,
                exit_code=result.exit_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                stdout=_trim(result.stdout),
                stderr=_trim(result.stderr),
                error=(f"timed out after {command.timeout_seconds:.0f}s" if result.timed_out else None),
            )
        except Exception as exc:
            return VerificationStepResult(
                name=command.name,
                command=list(argv),
                passed=False,
                exit_code=None,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=f"{type(exc).__name__}: {exc}",
            )

    def _patch_apply_step(self, patch: str) -> VerificationStepResult | None:
        if not patch or not (self.workspace_root / ".git").exists():
            return None
        started = time.perf_counter()
        checkout: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="run-agent-patch-check-") as temporary:
                checkout = Path(temporary) / "base"
                patch_file = Path(temporary) / "candidate.patch"
                # On Windows, passing a LF-only patch through text-mode stdin
                # can translate newlines before git sees them. Persist bytes
                # explicitly so the check validates the exact exported Patch.
                patch_file.write_bytes(patch.replace("\r\n", "\n").encode("utf-8"))
                created = subprocess.run(
                    ["git", "worktree", "add", "--detach", "--force", str(checkout), "HEAD"],
                    cwd=str(self.workspace_root), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60, shell=False,
                )
                if created.returncode != 0:
                    result = created
                else:
                    result = subprocess.run(
                        ["git", "apply", "--check", "--whitespace=nowarn", str(patch_file)],
                        cwd=str(checkout), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60, shell=False,
                    )
                    if result.returncode != 0:
                        # Windows text-mode subprocesses can normalize CRLF
                        # from a locally generated patch while the checked-out
                        # blob still contains CRLF. Retry only with Git's
                        # whitespace-context tolerance so line-ending-only
                        # differences do not veto an otherwise valid patch.
                        tolerant = subprocess.run(
                            [
                                "git", "apply", "--check", "--whitespace=nowarn",
                                "--ignore-space-change", "--ignore-whitespace", str(patch_file),
                            ],
                            cwd=str(checkout), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60, shell=False,
                        )
                        if tolerant.returncode == 0:
                            result = tolerant
            return VerificationStepResult(
                "patch-applies-to-base", ["git", "apply", "--check", "<base-commit>", "<patch-file>"],
                result.returncode == 0, result.returncode, round((time.perf_counter() - started) * 1000, 3),
                _trim(result.stdout or ""), _trim(result.stderr or ""),
            )
        except Exception as exc:
            return VerificationStepResult(
                "patch-applies-to-base", ["git", "apply", "--check", "<base-commit>", "<patch-file>"], False, None,
                round((time.perf_counter() - started) * 1000, 3), error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if checkout is not None:
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(checkout)],
                        cwd=str(self.workspace_root), capture_output=True, timeout=30, shell=False,
                    )
                    subprocess.run(
                        ["git", "worktree", "prune"],
                        cwd=str(self.workspace_root), capture_output=True, timeout=30, shell=False,
                    )
                except Exception:
                    pass

    async def verify(self, changed_paths: Iterable[str | Path], *, patch: str | None = None) -> VerificationReport:
        paths = tuple(sorted({str(Path(item).resolve()) for item in changed_paths}))
        root = self.workspace_root.resolve()
        outside = []
        for value in paths:
            try:
                Path(value).resolve().relative_to(root)
            except ValueError:
                outside.append(value)
        if outside:
            step = VerificationStepResult("workspace-boundary", [], False, None, 0.0, error=f"changed paths escape workspace: {outside}")
            return VerificationReport(False, (step,), paths, outcome="FAIL")
        if self.require_patch and not (patch or "").strip():
            return VerificationReport(False, changed_paths=paths, skipped_reason="no patch changes observed", outcome="FAIL")
        profile = self.profile or (self.sandbox.spec.verification_profile if self.sandbox is not None else "default")
        commands = (
            list(self.commands)
            if self.commands is not None
            else discover_verification_commands(self.workspace_root, paths, profile=profile)
        )
        if not paths and self.commands is None and self.require_patch:
            return VerificationReport(
                False,
                changed_paths=paths,
                skipped_reason="no patch changes observed",
                outcome="FAIL",
            )
        if not commands:
            return VerificationReport(
                False,
                changed_paths=paths,
                skipped_reason="no verification commands discovered",
                outcome="INCONCLUSIVE",
            )
        steps: list[VerificationStepResult] = []
        patch_step = self._patch_apply_step(patch or "")
        if patch_step is not None:
            steps.append(patch_step)
            if not patch_step.passed:
                outcome = (
                    "INFRASTRUCTURE_FAILURE"
                    if patch_step.exit_code is None and patch_step.error
                    else "FAIL"
                )
                return VerificationReport(False, tuple(steps), paths, outcome=outcome)
        for command in commands:
            step = (
                await self._run_step_in_sandbox(command)
                if self.sandbox is not None
                else await asyncio.to_thread(self._run_step, command)
            )
            steps.append(step)
            # Syntax failures make downstream tests noisy and rarely useful.
            if not step.passed and command.name.endswith("syntax"):
                break
        passed = all(step.passed for step in steps)
        outcome = "PASS" if passed else "FAIL"
        if any(step.exit_code is None and step.error for step in steps):
            outcome = "INFRASTRUCTURE_FAILURE"
        elif passed and self.require_focused_tests and not any(command.focused for command in commands):
            outcome = "INCONCLUSIVE"
            passed = False
            reason = "required static gates passed, but no focused test command was available"
            return VerificationReport(False, tuple(steps), paths, skipped_reason=reason, outcome=outcome)
        return VerificationReport(passed=passed, steps=tuple(steps), changed_paths=paths, outcome=outcome)
