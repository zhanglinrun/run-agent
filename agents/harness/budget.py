"""Explicit solve/repair budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn


class BudgetExceeded(RuntimeError):
    """Raised when a task has no remaining turn or token budget."""


@dataclass(frozen=True)
class BudgetSpec:
    total_turns: int = 18
    solve_turns: int = 14
    repair_turns: int = 4
    max_repair_attempts: int = 2
    max_input_tokens: int = 400_000
    max_output_tokens: int = 20_000
    max_cost_usd: float | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    def __post_init__(self) -> None:
        if self.total_turns < 1 or self.solve_turns < 0 or self.repair_turns < 0:
            raise ValueError("turn budgets must be non-negative and total_turns must be positive")
        if self.solve_turns + self.repair_turns > self.total_turns:
            raise ValueError("solve_turns + repair_turns cannot exceed total_turns")
        if self.max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must be non-negative")
        if self.max_input_tokens < 0 or self.max_output_tokens < 0:
            raise ValueError("token budgets must be non-negative")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative")
        rates = (self.input_cost_per_million, self.output_cost_per_million)
        if (rates[0] is None) != (rates[1] is None):
            raise ValueError("input and output cost rates must be configured together")
        if self.max_cost_usd is not None and rates[0] is None:
            raise ValueError("max_cost_usd requires configured input and output cost rates")
        if any(rate is not None and rate < 0 for rate in rates):
            raise ValueError("cost rates must be non-negative")


@dataclass
class BudgetLedger:
    spec: BudgetSpec
    solve_used: int = 0
    repair_used: int = 0
    repair_attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    exhausted_reason: str | None = None

    @property
    def turns_used(self) -> int:
        return self.solve_used + self.repair_used

    @property
    def solve_remaining(self) -> int:
        return max(0, self.spec.solve_turns - self.solve_used)

    @property
    def repair_remaining(self) -> int:
        return max(0, self.spec.repair_turns - self.repair_used)

    def _exceed(self, reason: str) -> NoReturn:
        self.exhausted_reason = self.exhausted_reason or reason
        raise BudgetExceeded(self.exhausted_reason)

    def ensure_available(self) -> None:
        if self.exhausted_reason is not None:
            raise BudgetExceeded(self.exhausted_reason)

    def remaining_for(self, phase: str) -> int:
        return self.repair_remaining if phase == "repair" else self.solve_remaining

    def ensure_turn_available(self, phase: str) -> None:
        self.ensure_available()
        if self.remaining_for(phase) <= 0:
            self._exceed(f"{phase} turn budget exhausted")

    def consume_turn(self, *, phase: str = "solve") -> None:
        self.ensure_turn_available(phase)
        if phase == "repair":
            self.repair_used += 1
        else:
            self.solve_used += 1

    def begin_repair_attempt(self) -> int:
        self.ensure_available()
        if self.repair_attempts >= self.spec.max_repair_attempts:
            self._exceed("repair attempt budget exhausted")
        if self.repair_remaining <= 0:
            self._exceed("repair turn budget exhausted")
        self.repair_attempts += 1
        return self.repair_attempts

    def consume_usage(self, *, input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None) -> None:
        self.ensure_available()
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        if self.input_tokens > self.spec.max_input_tokens:
            self._exceed("input token budget exhausted")
        if self.output_tokens > self.spec.max_output_tokens:
            self._exceed("output token budget exhausted")
        effective_cost = cost_usd
        if effective_cost is None and self.spec.input_cost_per_million is not None:
            effective_cost = (
                max(0, int(input_tokens)) * float(self.spec.input_cost_per_million)
                + max(0, int(output_tokens)) * float(self.spec.output_cost_per_million or 0.0)
            ) / 1_000_000
        if effective_cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + max(0.0, float(effective_cost))
            if self.spec.max_cost_usd is not None and self.cost_usd > self.spec.max_cost_usd:
                self._exceed("cost budget exhausted")

    def to_dict(self) -> dict[str, object]:
        return {
            "solve_used": self.solve_used,
            "repair_used": self.repair_used,
            "repair_attempts": self.repair_attempts,
            "turns_used": self.turns_used,
            "solve_remaining": self.solve_remaining,
            "repair_remaining": self.repair_remaining,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "exhausted_reason": self.exhausted_reason,
            "spec": {
                "total_turns": self.spec.total_turns,
                "solve_turns": self.spec.solve_turns,
                "repair_turns": self.spec.repair_turns,
                "max_repair_attempts": self.spec.max_repair_attempts,
                "max_input_tokens": self.spec.max_input_tokens,
                "max_output_tokens": self.spec.max_output_tokens,
                "max_cost_usd": self.spec.max_cost_usd,
                "input_cost_per_million": self.spec.input_cost_per_million,
                "output_cost_per_million": self.spec.output_cost_per_million,
            },
        }
