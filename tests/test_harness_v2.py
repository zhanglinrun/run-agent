from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from agents.extensions import ExtensionSpec
from agents.harness import (
    AgentHarness,
    BudgetSpec,
    ExtensionSettings,
    FailureKind,
    HarnessConfig,
    PermissionSettings,
    ProviderSettings,
    RuntimeConfig,
    SessionSettings,
    TaskSpec,
    TaskStatus,
    VerificationSettings,
)
from agents.providers.base import ModelResponse
from agents.runtime.contracts import ToolCall, ToolResult
from agents.runtime.core import AgentCore
from agents.session import EntryType, OperationType, SessionRepository, SessionReducer


_DISABLED_OPTIONAL = frozenset(
    {
        "plan",
        "context",
        "memory",
        "subagents",
        "skills",
        "skill-evolution",
        "mcp",
        "verification",
        "correction",
        "acceptance",
    }
)


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class FakeExecutor:
    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(call.id, call.name, "ok", True)


def _runtime(
    root: Path,
    provider: FakeProvider,
    *,
    disabled: frozenset[str] = _DISABLED_OPTIONAL,
    mode: str = "acceptEdits",
    verification: VerificationSettings | None = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        provider=ProviderSettings(adapter=provider),
        permissions=PermissionSettings(mode=mode),
        session=SessionSettings(
            database=root / ".run" / "sessions.db",
            trace_root=root / ".run" / "traces",
            artifact_dir=root / ".run" / "artifacts",
        ),
        verification=verification or VerificationSettings(),
        extensions=ExtensionSettings(disabled=disabled),
    )


def _git_fixture(root: Path, content: str = "value = 1\n") -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "module.py").write_text(content, encoding="utf-8")
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=root,
        check=True,
    )


@pytest.mark.asyncio
async def test_agent_core_runs_hooks_and_tool_calls() -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("c1", "read_file", {"file_path": "a.py"}),
                )
            ),
            ModelResponse(text="done", stop_reason="stop"),
        ]
    )
    core = AgentCore(provider=provider, tool_executor=FakeExecutor(), max_turns=3)
    result = await core.run("inspect")
    assert result.text == "done"
    assert result.tool_results[0].ok
    assert [item["role"] for item in result.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_setup_api_extension_changes_after_solve_outcome(tmp_path: Path) -> None:
    provider = FakeProvider([ModelResponse(text="done", stop_reason="stop")])

    def setup(api) -> None:
        async def after_solve(_event, context) -> None:
            context.outcome.final_text += " [extension]"

        api.on("after_solve", after_solve)

    result = await AgentHarness(
        HarnessConfig(
            session_db=tmp_path / "sessions.db",
            extensions=(ExtensionSpec("marker", setup),),
        )
    ).run(
        TaskSpec(
            "extension-stack",
            "inspect",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=1,
                solve_turns=1,
                repair_turns=0,
                max_repair_attempts=0,
            ),
            runtime=_runtime(tmp_path, provider),
        )
    )
    assert result.status == TaskStatus.COMPLETED
    assert result.answer.endswith("[extension]")
    assert result.verification is None


def test_sqlite_session_tree_preserves_duplicate_messages(tmp_path: Path) -> None:
    db = tmp_path / "session.db"
    with SessionRepository(db) as repository:
        session_id = repository.create_session(metadata={"task_id": "x"})
        first = repository.append_entry(
            session_id,
            "main",
            EntryType.MESSAGE,
            {"message": {"role": "user", "content": "same"}},
        )
        repository.append_entry(
            session_id,
            "main",
            EntryType.MESSAGE,
            {"message": {"role": "user", "content": "same"}},
            parent_id=first.id,
        )
        repository.append_operation(
            session_id,
            "main",
            OperationType.RUN_STARTED,
            {"task_id": "x"},
            run_id="run-1",
        )
        assert [item["content"] for item in SessionReducer(repository).messages(session_id)] == [
            "same",
            "same",
        ]


@pytest.mark.asyncio
async def test_verification_failure_is_unresolved_and_persisted(tmp_path: Path) -> None:
    _git_fixture(tmp_path)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("read", "read_file", {"file_path": "module.py"}),
                    ToolCall(
                        "write",
                        "write_file",
                        {"file_path": "module.py", "content": "value = 2\n"},
                    ),
                )
            ),
            ModelResponse(text="edited", stop_reason="stop"),
        ]
    )
    disabled = _DISABLED_OPTIONAL - {"verification"}
    verification = VerificationSettings(
        commands=((sys.executable, "-c", "raise SystemExit(1)"),)
    )
    result = await AgentHarness().run(
        TaskSpec(
            "verification-failure",
            "fix",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=2,
                solve_turns=2,
                repair_turns=0,
                max_repair_attempts=0,
            ),
            runtime=_runtime(
                tmp_path,
                provider,
                disabled=disabled,
                verification=verification,
            ),
        )
    )
    assert result.status == TaskStatus.UNRESOLVED
    assert result.patch
    assert result.verification and result.verification.outcome == "FAIL"


@pytest.mark.asyncio
async def test_correction_uses_separate_repair_budget(tmp_path: Path) -> None:
    _git_fixture(tmp_path)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("read", "read_file", {"file_path": "module.py"}),
                    ToolCall(
                        "write-2",
                        "write_file",
                        {"file_path": "module.py", "content": "value = 2\n"},
                    ),
                )
            ),
            ModelResponse(text="candidate 2", stop_reason="stop"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "write-3",
                        "write_file",
                        {"file_path": "module.py", "content": "value = 3\n"},
                    ),
                )
            ),
            ModelResponse(text="candidate 3", stop_reason="stop"),
        ]
    )
    disabled = _DISABLED_OPTIONAL - {"verification", "correction"}
    verification = VerificationSettings(
        commands=(
            (
                sys.executable,
                "-c",
                "ns = {}; exec(open('module.py', encoding='utf-8').read(), ns); assert ns['value'] == 3",
            ),
        ),
        disposable_workspace=True,
    )
    result = await AgentHarness().run(
        TaskSpec(
            "repair-pass",
            "set value to three",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=4,
                solve_turns=2,
                repair_turns=2,
                max_repair_attempts=1,
            ),
            runtime=_runtime(
                tmp_path,
                provider,
                disabled=disabled,
                verification=verification,
            ),
        )
    )
    assert result.status == TaskStatus.COMPLETED
    assert result.verification and result.verification.outcome == "PASS"
    assert len(result.correction_attempts) == 1
    assert result.metadata["budgets"]["solve_used"] == 2
    assert result.metadata["budgets"]["repair_used"] == 2


@pytest.mark.asyncio
async def test_side_query_budget_exhaustion_prevents_primary_request(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [ModelResponse(text="auxiliary", usage={"input": 2, "output": 0})]
    )

    def setup(api) -> None:
        async def before_run(_event, context) -> None:
            await context.side_query("auxiliary", "select context")

        api.on("before_run", before_run)

    result = await AgentHarness(
        HarnessConfig(extensions=(ExtensionSpec("budget-side-query", setup),))
    ).run(
        TaskSpec(
            "side-query-budget",
            "primary request",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=2,
                solve_turns=2,
                repair_turns=0,
                max_repair_attempts=0,
                max_input_tokens=1,
            ),
            runtime=_runtime(tmp_path, provider),
        )
    )
    assert len(provider.requests) == 1
    assert result.failure and result.failure.kind == FailureKind.BUDGET


@pytest.mark.asyncio
async def test_repair_usage_exhaustion_is_budget_failure(tmp_path: Path) -> None:
    _git_fixture(tmp_path)
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("read", "read_file", {"file_path": "module.py"}),
                    ToolCall(
                        "write-2",
                        "write_file",
                        {"file_path": "module.py", "content": "value = 2\n"},
                    ),
                )
            ),
            ModelResponse(text="candidate", stop_reason="stop"),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "write-3",
                        "write_file",
                        {"file_path": "module.py", "content": "value = 3\n"},
                    ),
                ),
                usage={"input": 2, "output": 0},
            ),
        ]
    )
    disabled = _DISABLED_OPTIONAL - {"verification", "correction"}
    result = await AgentHarness().run(
        TaskSpec(
            "repair-budget",
            "fix",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=3,
                solve_turns=2,
                repair_turns=1,
                max_repair_attempts=1,
                max_input_tokens=1,
            ),
            runtime=_runtime(
                tmp_path,
                provider,
                disabled=disabled,
                verification=VerificationSettings(
                    commands=((sys.executable, "-c", "raise SystemExit(1)"),)
                ),
            ),
        )
    )
    assert len(provider.requests) == 3
    assert result.failure and result.failure.kind == FailureKind.BUDGET


@pytest.mark.asyncio
async def test_plan_exit_requires_exact_approval(tmp_path: Path) -> None:
    _git_fixture(tmp_path)
    plan = tmp_path / ".run" / "plans" / "plan.md"
    provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "write",
                        "write_file",
                        {"file_path": str(plan), "content": "# Plan\n"},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall("search", "tool_search", {"query": "exit_plan_mode"}),
                )
            ),
            ModelResponse(
                tool_calls=(ToolCall("exit", "exit_plan_mode", {}),)
            ),
            ModelResponse(text="approved", stop_reason="stop"),
        ]
    )

    async def approve(_plan: str):
        return {"choice": "approve"}

    runtime = _runtime(tmp_path, provider, mode="plan")
    runtime = RuntimeConfig(
        provider=runtime.provider,
        execution=runtime.execution,
        permissions=PermissionSettings(
            mode="plan", plan_file=plan, plan_approval=approve
        ),
        prompt=runtime.prompt,
        session=runtime.session,
        verification=runtime.verification,
        extensions=ExtensionSettings(
            disabled=_DISABLED_OPTIONAL - {"plan"}
        ),
    )
    result = await AgentHarness().run(
        TaskSpec(
            "plan",
            "write and approve the plan",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=4,
                solve_turns=4,
                repair_turns=0,
                max_repair_attempts=0,
            ),
            runtime=runtime,
        )
    )
    assert result.status == TaskStatus.COMPLETED
    assert plan.read_text(encoding="utf-8") == "# Plan\n"


@pytest.mark.asyncio
async def test_harness_resumes_messages_from_sqlite(tmp_path: Path) -> None:
    db = tmp_path / ".run" / "sessions.db"
    first_provider = FakeProvider(
        [ModelResponse(text="first answer", stop_reason="stop")]
    )
    first = await AgentHarness().run(
        TaskSpec(
            "resume",
            "first question",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=1,
                solve_turns=1,
                repair_turns=0,
                max_repair_attempts=0,
            ),
            runtime=_runtime(tmp_path, first_provider),
        )
    )
    with SessionRepository(db) as repository:
        messages = SessionReducer(repository).messages(first.session_id)
    assert [item["content"] for item in messages] == [
        "first question",
        "first answer",
    ]

    second_provider = FakeProvider(
        [ModelResponse(text="second answer", stop_reason="stop")]
    )
    second_runtime = _runtime(tmp_path, second_provider)
    second_runtime = RuntimeConfig(
        provider=second_runtime.provider,
        execution=second_runtime.execution,
        permissions=second_runtime.permissions,
        prompt=second_runtime.prompt,
        session=SessionSettings(
            database=db,
            trace_root=second_runtime.session.trace_root,
            artifact_dir=second_runtime.session.artifact_dir,
            resume_session_id=first.session_id,
        ),
        verification=second_runtime.verification,
        extensions=second_runtime.extensions,
    )
    second = await AgentHarness().run(
        TaskSpec(
            "resume",
            "second question",
            tmp_path,
            mode="interactive",
            budget=BudgetSpec(
                total_turns=1,
                solve_turns=1,
                repair_turns=0,
                max_repair_attempts=0,
            ),
            runtime=second_runtime,
        )
    )
    assert second.session_id == first.session_id
    assert [item["content"] for item in second_provider.requests[0].messages] == [
        "first question",
        "first answer",
        "second question",
    ]
