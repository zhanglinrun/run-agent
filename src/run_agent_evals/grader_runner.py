"""Run a task's own grader and report one outcome per test.

Admission answers "did the grader pass" with a single exit code. The dual
propositions need more than that: they need to know which tests changed verdict, so
that a fix can be proven and a regression caught - a single code cannot tell a
solved target from a newly broken neighbour.

This runs the same isolated grading directory admission builds - grader assets come
from the task directory, never from the workspace, so a solution cannot reach the
tests that grade it - and asks pytest for a JUnit report instead of parsing console
output. Skipped tests are reported as absent rather than passing, because a test that
did not run is not evidence that anything holds.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from run_agent_evals.task_spec import TaskSpec, run_grader, seed_grading_directory

JUNIT_FLAG = "--junit-xml"
DISABLE_CACHE = ("-p", "no:cacheprovider")


@dataclass(frozen=True, slots=True)
class GraderSuiteRunner:
    """The ``SuiteRunner`` capability backed by one task's grader."""

    spec: TaskSpec

    async def run(self, workspace: Path) -> Mapping[str, bool]:
        """Run the grader off the event loop and return per-test outcomes."""
        return await asyncio.to_thread(self.run_blocking, Path(workspace))

    def run_blocking(self, workspace: Path) -> Mapping[str, bool]:
        """Seed the grading directory, run the grader, parse its JUnit report.

        A workspace missing a declared artifact raises: that is an unusable
        solution or a broken task, not a task outcome, and hiding it as "no
        evidence" would make the two indistinguishable. A grader that exceeds its
        budget yields no per-test evidence, which the propositions then treat as
        unproven.
        """
        container = Path(tempfile.mkdtemp(prefix=f"suite-{self.spec.id}-"))
        destination = container / "grading"
        report = container / "results.xml"
        seed_grading_directory(self.spec, workspace, destination)
        try:
            run_grader(self.spec, destination, (JUNIT_FLAG, str(report), *DISABLE_CACHE))
        except subprocess.TimeoutExpired:
            return {}
        return parse_junit(report)


def parse_junit(report: Path) -> Mapping[str, bool]:
    """Return ``classname::name -> passed`` for every test that actually ran."""
    if not report.exists():
        return {}
    outcomes: dict[str, bool] = {}
    for case in ET.parse(report).iter("testcase"):
        if case.find("skipped") is not None:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        broke = case.find("failure") is not None or case.find("error") is not None
        outcomes[name] = not broke
    return outcomes
