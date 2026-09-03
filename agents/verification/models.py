"""Typed verification results used by the Runtime and Trace."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class VerificationStepResult:
    name: str
    command: list[str]
    passed: bool
    exit_code: int | None
    duration_ms: float
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    steps: tuple[VerificationStepResult, ...] = field(default_factory=tuple)
    changed_paths: tuple[str, ...] = field(default_factory=tuple)
    skipped_reason: str = ""
    outcome: str = "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "outcome": self.outcome,
            "steps": [item.to_dict() for item in self.steps],
            "changed_paths": list(self.changed_paths),
            "skipped_reason": self.skipped_reason,
            "fingerprint": self.fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        failures = [
            {
                "name": step.name,
                "exit_code": step.exit_code,
                "stderr": step.stderr[-2000:],
                "stdout": step.stdout[-2000:],
                "error": step.error,
            }
            for step in self.steps
            if not step.passed
        ]
        raw = json.dumps(failures, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def feedback(self) -> str:
        if self.passed:
            return "All runtime verification steps passed."
        lines = [f"Runtime verification outcome: {self.outcome}. Diagnose the evidence and repair the implementation:"]
        if self.skipped_reason:
            lines.append(f"\nGate note: {self.skipped_reason}")
        for step in self.steps:
            if step.passed:
                continue
            lines.append(f"\n## {step.name}\nCommand: {' '.join(step.command)}\nExit code: {step.exit_code}")
            evidence = (step.stderr or step.stdout or step.error or "no diagnostic output").strip()
            lines.append(evidence[-6000:])
        lines.append("\nDo not claim completion until the verification gate passes.")
        return "\n".join(lines)
