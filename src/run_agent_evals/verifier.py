"""The two propositions that make "fixed" a checkable claim (chapter 7, T-052/T-053).

Chapter 7 requires both sides of a fix to be proven independently:

    FAIL_TO_PASS  failed before, passes after  -> the problem is really solved
    PASS_TO_PASS  passed before and after      -> nothing else was broken

Checking only the first lets an agent delete or edit the assertions in its way;
checking only the second is not checking at all. This module also refuses to treat
a vanished test as a passing one, which is the cheapest form of that deletion, and
excludes tests that pass and fail at random - a verdict resting on a flaky test is
not a verdict.

Classification is pure: it takes what a suite reported on two states and decides.
Running the suite is an external capability supplied at the boundary, so the logic
here stays deterministic and free of subprocesses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SuiteResult:
    """Per-test outcomes from one or more runs against a single unchanged state.

    Repeats are what make stability decidable: a test whose outcome differs between
    runs on the same state is flaky, and chapter 7 says to exclude it rather than
    let it decide a verdict.
    """

    runs: tuple[Mapping[str, bool], ...]

    def __post_init__(self) -> None:
        if not self.runs:
            raise ValueError("A suite result needs at least one run")

    @classmethod
    def of(cls, runs: tuple[Mapping[str, bool], ...]) -> SuiteResult:
        return cls(runs=runs)

    @classmethod
    def of_single(cls, outcomes: Mapping[str, bool]) -> SuiteResult:
        return cls(runs=(outcomes,))

    @property
    def _test_ids(self) -> frozenset[str]:
        return frozenset().union(*(frozenset(run) for run in self.runs))

    @property
    def flaky(self) -> frozenset[str]:
        """Tests whose outcome was not the same in every run."""
        return frozenset(
            test_id
            for test_id in self._test_ids
            if len({run.get(test_id) for run in self.runs}) > 1
        )

    @property
    def unanimous(self) -> Mapping[str, bool]:
        """Outcomes that held in every run, with the flaky ones dropped."""
        unstable = self.flaky
        return {
            test_id: bool(self.runs[0][test_id])
            for test_id in self._test_ids - unstable
            if test_id in self.runs[0]
        }

    @property
    def passing(self) -> frozenset[str]:
        return frozenset(t for t, ok in self.unanimous.items() if ok)

    @property
    def failing(self) -> frozenset[str]:
        return frozenset(t for t, ok in self.unanimous.items() if not ok)


@dataclass(frozen=True)
class DualProposition:
    """Both propositions, plus the ways each can fail, for one candidate change.

    ``targets`` is the set of tests the task declares must go from failing to passing.
    Without it a candidate that flips one incidental test would count as a proven fix,
    which is why the declared set, not the observed one, decides ``is_fix_proven``.
    """

    fail_to_pass: frozenset[str]
    pass_to_pass: frozenset[str]
    newly_failing: frozenset[str]
    still_failing: frozenset[str]
    flaky: frozenset[str]
    targets: frozenset[str] = frozenset()

    @property
    def unmet_targets(self) -> frozenset[str]:
        """Declared targets that did not go from failing to passing."""
        met = self.fail_to_pass
        return frozenset(
            target for target in self.targets if not any(k.endswith(f"::{target}") for k in met)
        )

    @property
    def is_fix_proven(self) -> bool:
        if self.targets:
            return not self.unmet_targets
        return bool(self.fail_to_pass)

    @property
    def is_regression_free(self) -> bool:
        return not self.newly_failing

    @property
    def succeeded(self) -> bool:
        return self.is_fix_proven and self.is_regression_free


def classify(
    pristine: SuiteResult, candidate: SuiteResult, targets: Iterable[str] = ()
) -> DualProposition:
    """Decide both propositions from the suite's report on two states.

    ``pristine`` is the task before any change, ``candidate`` the workspace the agent
    produced. A test the candidate no longer reports is counted as newly failing
    rather than dropped, so removing an inconvenient assertion cannot read as a fix.
    """
    missing = pristine.unanimous.keys() - candidate.unanimous.keys()
    vanished = pristine.passing & frozenset(missing)
    no_longer_reported = pristine.failing & frozenset(missing)
    return DualProposition(
        fail_to_pass=pristine.failing & candidate.passing,
        pass_to_pass=pristine.passing & candidate.passing,
        newly_failing=(pristine.passing & candidate.failing) | vanished,
        still_failing=(pristine.failing & candidate.failing) | no_longer_reported,
        flaky=pristine.flaky | candidate.flaky,
        targets=frozenset(targets),
    )


class SuiteRunner(Protocol):
    """Capability that reports per-test outcomes for a workspace.

    Declared at the boundary so the real grader and a test double are
    interchangeable, and so classification never needs a subprocess.
    """

    async def run(self, workspace: Path) -> Mapping[str, bool]: ...


@dataclass(frozen=True)
class DualPropositionVerifier:
    """Verify a change by running the suite on the pristine and candidate states."""

    runner: SuiteRunner
    repeats: int = 3

    async def _observe(self, workspace: Path) -> SuiteResult:
        runs: list[Mapping[str, bool]] = []
        for _ in range(self.repeats):
            runs.append(await self.runner.run(workspace))
        return SuiteResult.of(runs=tuple(runs))

    async def verify(
        self, pristine: Path, candidate: Path, targets: Iterable[str] = ()
    ) -> DualProposition:
        return classify(
            await self._observe(pristine), await self._observe(candidate), targets=targets
        )
