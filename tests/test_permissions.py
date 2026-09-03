from pathlib import Path

from agents.policy import PolicyEngine, WorkspaceBoundary


def _engine(root: Path) -> PolicyEngine:
    return PolicyEngine(WorkspaceBoundary(root))


def test_plan_mode_denies_shell(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "run_shell", {"command": "echo unsafe"}, mode="plan"
    )
    assert decision.action == "deny"


def test_read_tools_are_allowed_in_plan_mode(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "read_file", {"file_path": "README.md"}, mode="plan"
    )
    assert decision.action == "allow"


def test_plan_mode_only_allows_exact_plan_file(tmp_path: Path) -> None:
    plan = tmp_path / ".run" / "plans" / "plan.md"
    allowed = _engine(tmp_path).decide(
        "write_file",
        {"file_path": str(plan), "content": "plan"},
        mode="plan",
        plan_file_path=str(plan),
    )
    denied = _engine(tmp_path).decide(
        "write_file",
        {"file_path": str(tmp_path / "source.py"), "content": "x"},
        mode="plan",
        plan_file_path=str(plan),
    )
    assert allowed.action == "allow"
    assert denied.action == "deny"


def test_external_mcp_tools_require_confirmation_by_default(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "mcp__github__create_issue", {"title": "x"}, mode="default"
    )
    assert decision.action == "confirm"


def test_external_mcp_tools_are_denied_in_ci_mode(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "mcp__github__create_issue", {"title": "x"}, mode="dontAsk"
    )
    assert decision.action == "deny"


def test_safe_shell_prefix_cannot_hide_a_compound_command(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "run_shell",
        {"command": "git status; python -c \"open('owned.txt', 'w').write('x')\""},
        mode="default",
    )
    assert decision.action == "confirm"
    assert decision.risk == "unknown"


def test_verification_shell_chain_is_not_auto_allowed(tmp_path: Path) -> None:
    decision = _engine(tmp_path).decide(
        "run_shell",
        {"command": "pytest -q && python -c \"open('owned.txt', 'w').write('x')\""},
        mode="dontAsk",
    )
    assert decision.action == "deny"
    assert decision.risk == "unknown"


def test_unknown_extension_tool_fails_closed_in_ci(tmp_path: Path) -> None:
    assert _engine(tmp_path).decide("extension_write", {}, mode="dontAsk").action == "deny"
