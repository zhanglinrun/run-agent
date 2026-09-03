"""Typed SWE-bench task and prediction contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SWEBenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    eval_script: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    image: str
    version: str
    environment_setup_commit: str = ""
    difficulty: str = ""
    eval_type: str = ""
    hints_text: str = ""
    log_parser: str = ""
    gold_patch: str = ""

    def prompt(self) -> str:
        hints = self.hints_text.strip()
        extra = f"\n\nIssue hints:\n{hints}" if hints else ""
        return (
            f"You are fixing instance {self.instance_id} in repository {self.repo}.\n\n"
            "Read the repository and implement the requested fix. Work only in the "
            "current checkout. Use typed file tools and verification commands. Do not "
            "stop at an explanation: modify the code and run focused tests when possible.\n\n"
            "This is an offline Docker SWE-bench task. Do not run pip/conda install, "
            "network probes, or broad environment searches. Use the image's testbed "
            "Python at /opt/miniconda3/envs/testbed/bin/python. For Django-style "
            "repositories, run the focused tests with tests/runtests.py. Avoid dumping "
            "full-file contents or repeating git status/diff commands. Once the minimal "
            "patch is implemented and a focused check passes, finish the task.\n\n"
            f"Issue statement:\n{self.problem_statement.strip()}{extra}\n\n"
            "When complete, summarize the change and tests run."
        )

    def prediction(self, patch: str, *, model_name: str) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }


__all__ = ["SWEBenchInstance"]
