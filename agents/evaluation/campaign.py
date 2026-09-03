"""Shared configuration for coding and SWE-bench campaigns."""

from __future__ import annotations

import argparse

from ..harness import BudgetSpec


def campaign_budget(
    args: argparse.Namespace,
    *,
    default_input_rate: float | None = None,
    default_output_rate: float | None = None,
) -> BudgetSpec:
    total = max(1, int(getattr(args, "max_turns", 18) or 18))
    solve = min(14, total)
    repair = min(4, max(0, total - solve))
    input_rate = getattr(args, "input_cost_per_million", default_input_rate)
    output_rate = getattr(args, "output_cost_per_million", default_output_rate)
    if input_rate is None:
        input_rate = default_input_rate
    if output_rate is None:
        output_rate = default_output_rate
    return BudgetSpec(
        total_turns=total,
        solve_turns=solve,
        repair_turns=repair,
        max_repair_attempts=min(
            2, max(0, int(getattr(args, "max_repair_attempts", 2) or 0))
        ),
        max_cost_usd=getattr(args, "max_cost", None),
        input_cost_per_million=input_rate,
        output_cost_per_million=output_rate,
    )


def disabled_harness_extensions(
    harness_flags: dict[str, bool], *, include_plan: bool = False
) -> frozenset[str]:
    disabled = {"memory", "skills", "skill-evolution", "mcp"}
    if include_plan:
        disabled.add("plan")
    if not harness_flags.get("verification", False):
        disabled.update({"verification", "correction"})
    elif not harness_flags.get("correction", False):
        disabled.add("correction")
    return frozenset(disabled)
