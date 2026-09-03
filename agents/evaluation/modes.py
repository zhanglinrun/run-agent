"""Shared Harness ablation modes used by coding evaluators."""

from __future__ import annotations


HARNESS_MODES: dict[str, dict[str, bool]] = {
    "baseline": {"verification": False, "correction": False},
    "verifier": {"verification": True, "correction": False},
    "full": {"verification": True, "correction": True},
}


__all__ = ["HARNESS_MODES"]
