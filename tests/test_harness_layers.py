from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from agents.collaboration import get_role_spec
from agents.runtime.contracts import EvalCase
from agents.evaluation.verifiers import verify_trace
from agents.evaluation.coding import load_coding_tasks
from agents.evaluation.model_config import resolve_campaign_model
from agents.evolution import PromotionEvidence, promote_candidate, stage_skill_candidate
from agents.tools.mcp import build_mcp_child_env
from agents.providers import decode_openai_tool_arguments
from agents.policy import PolicyEngine, WorkspaceBoundary
from agents.tools import ToolValidationError, tool_definitions, validate_tool_input
from agents.verification.discovery import VerificationCommand
from agents.verification.discovery import discover_verification_commands
from agents.verification import VerificationOrchestrator


def test_plan_mode_cannot_be_overridden_by_allow_rule(tmp_path: Path) -> None:
    engine = PolicyEngine(WorkspaceBoundary(tmp_path))
    decision = engine.decide(
        "run_shell",
        {"command": "Remove-Item victim.txt"},
        mode="plan",
        rule_result="allow",
    )
    assert decision.action == "deny"
    assert decision.reason_code == "plan_mode"


def test_shell_mutation_cannot_bypass_typed_file_policy(tmp_path: Path) -> None:
    engine = PolicyEngine(WorkspaceBoundary(tmp_path))
    decision = engine.decide(
        "run_shell",
        {"command": "Set-Content victim.txt changed"},
        mode="dontAsk",
    )
    assert decision.action == "deny"
    assert decision.risk == "workspace_write"


def test_schema_validation_precedes_tool_policy() -> None:
    with pytest.raises(ToolValidationError):
        validate_tool_input("read_file", {}, tool_definitions)


def test_workspace_boundary_rejects_outside_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    decision = PolicyEngine(WorkspaceBoundary(workspace)).decide(
        "read_file",
        {"file_path": str(outside)},
        mode="bypassPermissions",
    )
    assert decision.action == "deny"
    assert decision.reason_code == "workspace_boundary"


def test_mcp_child_environment_does_not_inherit_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PATH", "safe-path")
    env = build_mcp_child_env()
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "safe-path"
    explicit = build_mcp_child_env({"SERVICE_API_KEY": "configured"})
    assert explicit["SERVICE_API_KEY"] == "configured"


def test_mcp_server_start_is_denied_before_process_launch(tmp_path: Path) -> None:
    decision = PolicyEngine(WorkspaceBoundary(tmp_path)).decide(
        "mcp_server_start",
        {"summary": "python evil.py"},
        mode="dontAsk",
    )
    assert decision.action == "deny"
    assert decision.reason_code == "mcp_start"


def test_unknown_extension_tool_requires_confirmation(tmp_path: Path) -> None:
    engine = PolicyEngine(WorkspaceBoundary(tmp_path))
    assert engine.decide("custom_tool", {}, mode="default").action == "confirm"
    assert engine.decide("custom_tool", {}, mode="dontAsk").action == "deny"


def test_malformed_openai_arguments_are_not_silently_empty() -> None:
    value = decode_openai_tool_arguments("{not-json")
    assert "__tool_input_error__" in value


def test_fake_trace_missing_decision_and_result_fails() -> None:
    case = EvalCase(
        case_id="fake",
        prompt="x",
        expected={"exact_answer": "ok", "required_tools": ["read_file"]},
    )
    events = [
        {"event_id": "1", "run_id": "r", "sequence": 1, "type": "run.started", "payload": {}},
        {"event_id": "2", "run_id": "r", "sequence": 2, "type": "tool.requested", "payload": {"call_id": "c", "name": "read_file"}},
        {"event_id": "3", "run_id": "r", "sequence": 3, "type": "run.completed", "payload": {"answer": "ok"}},
    ]
    result = verify_trace(case, events)
    assert not result.passed
    assert any(item["name"] == "single_result_for:c" and not item["passed"] for item in result.checks)


@pytest.mark.asyncio
async def test_verification_orchestrator_returns_environment_evidence(tmp_path: Path) -> None:
    command = VerificationCommand("forced-failure", (sys.executable, "-c", "import sys; print('boom'); sys.exit(2)"), 10)
    report = await VerificationOrchestrator(workspace_root=tmp_path, commands=[command]).verify([])
    assert not report.passed
    assert report.steps[0].exit_code == 2
    assert "boom" in report.steps[0].stdout


@pytest.mark.asyncio
async def test_local_verifier_launch_failure_is_infrastructure(tmp_path: Path) -> None:
    command = VerificationCommand(
        "missing-verifier",
        ("run-agent-command-that-does-not-exist",),
        5,
    )
    report = await VerificationOrchestrator(
        workspace_root=tmp_path,
        commands=[command],
    ).verify([])
    assert report.outcome == "INFRASTRUCTURE_FAILURE"
    assert report.steps[0].exit_code is None
    assert report.steps[0].error


def test_python_swebench_profile_skips_incidental_npm_checks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text('{"scripts":{"test":"echo test"}}', encoding="utf-8")
    changed = tmp_path / "module.py"
    changed.write_text("value = 1\n", encoding="utf-8")

    commands = discover_verification_commands(tmp_path, [changed], profile="python-swebench")

    assert {command.name for command in commands} == {"python-syntax", "pytest"}


@pytest.mark.asyncio
async def test_sandbox_verification_uses_testbed_python(tmp_path: Path) -> None:
    from agents.execution import SandboxSpec
    from tests.fakes import FakeSandboxBackend

    backend = FakeSandboxBackend(results=[
        # The first command is python-syntax and should use the configured
        # image interpreter rather than the host interpreter.
        __import__("agents.execution", fromlist=["ExecResult"]).ExecResult(
            ("/opt/miniconda3/envs/testbed/bin/python",), 0, "ok", "", False, 1.0
        )
    ])
    session = await backend.start(SandboxSpec(
        workspace=tmp_path,
        container_workspace="/testbed",
        python_executable="/opt/miniconda3/envs/testbed/bin/python",
        verification_profile="python-swebench",
    ))
    command = VerificationCommand("python-syntax", (sys.executable, "-m", "py_compile"), 5)
    report = await VerificationOrchestrator(
        workspace_root=tmp_path,
        commands=[command],
        sandbox=session,
    ).verify([])

    assert report.passed
    assert session.requests[0].argv[0] == "/opt/miniconda3/envs/testbed/bin/python"


def test_skill_candidate_requires_all_promotion_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    staged = stage_skill_candidate(candidate={"name": "safe-skill"}, proposed_action="add")
    rejected = promote_candidate(
        staged["candidate_id"],
        PromotionEvidence(replay_pass=True, boundary_pass=False, retention_pass=True),
    )
    assert rejected["action"] == "rejected"

    staged = stage_skill_candidate(
        candidate={
            "name": "safe-skill-v2",
            "description": "Use for safe Skill candidate checks.",
            "instructions": "# Workflow\n\n1. Apply the verified rule.",
        },
        proposed_action="add",
    )
    promoted = promote_candidate(
        staged["candidate_id"],
        PromotionEvidence(replay_pass=True, boundary_pass=True, retention_pass=True),
    )
    assert promoted["action"] == "promoted"
    assert Path(promoted["activation"]["file"]).exists()


def test_collaboration_is_limited_to_three_roles() -> None:
    assert get_role_spec("coder").name == "coder"
    assert get_role_spec("reviewer").allowed_tools == get_role_spec("verifier").allowed_tools
    with pytest.raises(KeyError):
        get_role_spec("manager")


def test_coding_smoke_manifest_points_to_failing_fixtures() -> None:
    root = Path(__file__).resolve().parents[1]
    tasks = load_coding_tasks(root / "evals" / "coding" / "smoke" / "tasks.jsonl")
    assert len(tasks) == 2
    for task in tasks:
        assert Path(task.fixture).is_dir()
        command = list(task.verify[0])
        result = subprocess.run(
            command,
            cwd=task.fixture,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        assert result.returncode != 0, f"fixture must start failing: {task.task_id}"


def test_coding_parser_supports_no_api_adapter_smoke() -> None:
    from agents.evaluation.coding import build_parser

    args = build_parser().parse_args([
        "evals/coding/smoke/tasks.jsonl",
        "--adapter-only",
        "--sandbox",
        "local",
    ])
    assert args.adapter_only is True
    assert args.max_cost is None


def test_evaluation_model_selection_requires_explicit_live_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("MODEL_PRIMARY", raising=False)
    monkeypatch.delenv("MODEL_SMOKE", raising=False)
    with pytest.raises(RuntimeError, match="requires an explicit model ID"):
        resolve_campaign_model(None)
    assert resolve_campaign_model("gpt-5.5") == "gpt-5.5"
    monkeypatch.setenv("MODEL_PRIMARY", "gpt-5.5")
    assert resolve_campaign_model(None) == "gpt-5.5"
    assert resolve_campaign_model(None, adapter_only=True) == "adapter-only"


@pytest.mark.asyncio
async def test_coding_adapter_never_modifies_original_fixture(tmp_path: Path) -> None:
    import hashlib
    from agents.evaluation.coding import build_parser, run_coding_campaign

    root = Path(__file__).resolve().parents[1]
    fixture = root / "evals" / "coding" / "smoke" / "fixtures" / "python-off-by-one"
    tracked = [fixture / "range_sum.py", fixture / "tests" / "test_range_sum.py", fixture / "pyproject.toml"]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
    args = build_parser().parse_args([
        str(root / "evals" / "coding" / "smoke" / "tasks.jsonl"),
        "--limit", "1",
        "--adapter-only",
        "--sandbox", "local",
        "--output", str(tmp_path / "runs"),
    ])

    run_dir = await run_coding_campaign(args)

    assert run_dir.is_dir()
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
