"""Evidence-driven repair strategy selection for the Harness."""

from __future__ import annotations

from dataclasses import dataclass

from ..verification import VerificationReport


@dataclass(frozen=True)
class CorrectionDecision:
    action: str
    reason: str
    repeated: bool
    fingerprint_count: int


class CorrectionController:
    def __init__(self, *, repeated_failure_limit: int = 2) -> None:
        self.repeated_failure_limit = max(1, int(repeated_failure_limit))
        self._fingerprints: dict[str, int] = {}

    def decide(self, report: VerificationReport, *, has_restorable_candidate: bool) -> CorrectionDecision:
        fingerprint = report.fingerprint
        self._fingerprints[fingerprint] = self._fingerprints.get(fingerprint, 0) + 1
        count = self._fingerprints[fingerprint]
        repeated = count >= self.repeated_failure_limit
        if repeated and has_restorable_candidate:
            return CorrectionDecision(
                "restore_best_and_change_strategy",
                "repeated failure fingerprint; restore the best verified candidate and change localization strategy",
                True,
                count,
            )
        return CorrectionDecision(
            "retry_in_place",
            "inject structured verification evidence into the bounded repair turn",
            repeated,
            count,
        )
