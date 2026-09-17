"""Campaign isolation and complete-denominator reduction, without paid inference."""

import argparse
import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_swebench_all_extensions.py"
spec = importlib.util.spec_from_file_location("swe_campaign", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def tasks():
    return [
        {
            "instance_id": f"django__django-{i}",
            "repo": "django/django",
            "base_commit": "a" * 40,
            "problem_statement": f"issue {i}",
            "patch": "DO NOT COPY GOLD",
            "test_patch": "DO NOT COPY TESTS",
        }
        for i in range(50)
    ]


def test_sanitized_inputs_and_150_distinct_trials():
    safe = runner.sanitize_tasks(tasks())
    assert all(set(task) == set(runner.SAFE_FIELDS) for task in safe)
    assert "DO NOT COPY" not in str(safe)
    trials = runner.trials(safe, 3)
    assert len({(sample, row["instance_id"]) for sample, row in trials}) == 150
    with pytest.raises(ValueError):
        runner.sanitize_tasks(tasks()[:-1])


def test_errors_and_empty_patches_remain_in_denominator():
    scores = runner.reduce_scores(["a", "b", "empty", "error"], [{"a", "b"}, {"a"}, {"a"}])
    assert scores["total_trials"] == 12
    assert scores["pass_at_1"] == 4 / 12
    assert scores["pass_at_3"] == 2 / 4
    assert scores["pass_cubed"] == 1 / 4


def test_prepare_refuses_old_artifacts(tmp_path):
    old = tmp_path / "campaign"
    old.mkdir()
    receipt = old / "result.json"
    receipt.write_text("historical", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runner.prepare(argparse.Namespace(campaign=old))
    assert receipt.read_text(encoding="utf-8") == "historical"


def test_required_extension_policy_and_secret_redaction():
    assert set(runner.EXTENSIONS) == {"mcp", "experience", "permission_policy", "plan_mode"}
    assert runner.POLICY["RUN_AGENT_PERMISSION_MODE"] == "yolo"
    assert "RUN_AGENT_MCP_SERVERS" not in runner.POLICY
    assert runner.POLICY["EXPERIENCE_REVIEW_ENABLED"] == "true"
    assert (
        runner.redact("credential-secret", {"OPENAI_API_KEY": "credential-secret"}) == "[REDACTED]"
    )


def test_patch_keeps_new_source_and_excludes_harness_artifacts(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    runner.run(["git", "init"], cwd=work)
    runner.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=work)
    runner.run(["git", "config", "user.name", "Fixture"], cwd=work)
    (work / "source.py").write_text("old\n", encoding="utf-8")
    runner.run(["git", "add", "source.py"], cwd=work)
    runner.run(["git", "commit", "-m", "fixture"], cwd=work)
    base = runner.run(["git", "rev-parse", "HEAD"], cwd=work).strip()
    (work / "source.py").write_text("new\n", encoding="utf-8")
    (work / "new_solution.py").write_text("new solution\n", encoding="utf-8")
    (work / ".run").mkdir()
    (work / ".run/MEMORY.md").write_text("private memory", encoding="utf-8")
    patch = runner.extract_patch(work)
    assert "+new" in patch and "new_solution.py" in patch
    assert "MEMORY" not in patch and "private memory" not in patch
    assert not runner.run(["git", "diff", "--cached"], cwd=work)
    runner.run(["git", "add", "source.py", "new_solution.py"], cwd=work)
    runner.run(["git", "commit", "-m", "solver commit"], cwd=work)
    committed_patch = runner.extract_patch(work, base)
    assert "+new" in committed_patch and "new_solution.py" in committed_patch
    assert "MEMORY" not in committed_patch


def test_solver_pip_uses_private_environment(tmp_path):
    config = {"repo_root": str(tmp_path), "environment": dict(runner.POLICY)}
    trial = tmp_path / "trial"
    env = runner.environment(config, trial)
    assert env["PATH"].split(os.pathsep)[0] == str(trial / "tool-env/Scripts")
    assert env["VIRTUAL_ENV"] == str(trial / "tool-env")
    assert env["PIP_REQUIRE_VIRTUALENV"] == "true"


def test_trial_repository_hides_future_answers_and_extracts_patch(tmp_path):
    evidence = runner.probe_git_isolation(tmp_path)
    source = tmp_path / "synthetic-cache"
    work = tmp_path / "work"
    base = evidence["base_commit"]
    future = runner.run(["git", "rev-parse", "future-answer"], cwd=source).strip()
    assert runner.run(["git", "log", "--all", "--format=%H"], cwd=work).split() == [base]
    assert (work / "source.txt").read_text(encoding="utf-8") == "base\n"
    for revision in (future, "future-answer", f"{future}:source.txt", "HEAD^"):
        with pytest.raises(RuntimeError):
            runner.run(["git", "show", revision], cwd=work)
    assert not runner.run(["git", "remote", "-v"], cwd=work).strip()
    assert not runner.run(["git", "for-each-ref"], cwd=work).strip()
    assert not (work / ".git/objects/info/alternates").exists()
    assert not (work / ".git/FETCH_HEAD").exists()
    assert str(source) not in (work / ".git/config").read_text(encoding="utf-8")
    assert runner.run(["git", "rev-parse", "--is-shallow-repository"], cwd=work).strip() == "true"
    (work / "source.txt").write_text("solver change\n", encoding="utf-8")
    (work / "new_solution.py").write_text("new solution\n", encoding="utf-8")
    patch = runner.extract_patch(work, base)
    assert "+solver change" in patch and "new_solution.py" in patch
    assert not runner.run(["git", "diff", "--cached"], cwd=work)
    assert runner.run(["git", "rev-parse", "HEAD"], cwd=source).strip() == future
    assert not runner.run(["git", "status", "--porcelain"], cwd=source).strip()
    with pytest.raises(FileExistsError):
        runner.prepare_repository(source, work, base)


def test_stream_archive_retains_deltas_without_repeating_growing_messages():
    from run_agent_core.events import MessageEndEvent, MessageUpdateEvent
    from run_agent_core.messages import AssistantMessage
    from run_agent_core.provider_events import TextDeltaEvent

    message = AssistantMessage(content="answer " * 10000)
    event = MessageUpdateEvent(
        message=message,
        assistant_message_event=TextDeltaEvent(content_index=0, delta="answer", partial=message),
    )
    recorded = runner.event_record(event)
    assert recorded["assistant_message_event"]["delta"] == "answer"
    assert "partial" not in recorded["assistant_message_event"]
    assert len(str(recorded)) < 500
    assert runner.event_record(MessageEndEvent(message=message))["message"]["content"]
