from __future__ import annotations

from pathlib import Path
import ast
import hashlib
import json

import pytest

from agents.harness import (
    AgentHarness,
    BudgetSpec,
    ExtensionSettings,
    ProviderSettings,
    RuntimeConfig,
    SessionSettings,
    TaskSpec,
)
from agents.providers.base import ModelResponse
from agents.runtime.tracing import load_trace


EXPECTED_AGENT_PACKAGES = {
    "app",
    "collaboration",
    "context",
    "correction",
    "execution",
    "evaluation",
    "evolution",
    "extensions",
    "harness",
    "policy",
    "providers",
    "runtime",
    "session",
    "tools",
    "verification",
}
EXPECTED_ROOT_MODULES = {"__init__.py", "__main__.py", "cli.py"}


def test_agents_root_matches_harness_architecture() -> None:
    root = Path(__file__).resolve().parents[1] / "agents"
    packages = {path.name for path in root.iterdir() if path.is_dir() and (path / "__init__.py").exists()}
    modules = {path.name for path in root.glob("*.py")}

    assert packages == EXPECTED_AGENT_PACKAGES
    assert modules == EXPECTED_ROOT_MODULES
    assert not (root / "tools" / "legacy.py").exists()


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            values.add(prefix + (node.module or ""))
    return values


def test_runtime_has_no_harness_layer_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "agents" / "runtime"
    forbidden = (
        "..context",
        "..policy",
        "..verification",
        "..correction",
        "..evolution",
        "..evaluation",
        "..extensions",
        "..harness",
    )
    violations = {
        str(path): sorted(name for name in _imports(path) if name.startswith(forbidden))
        for path in root.glob("*.py")
        if any(name.startswith(forbidden) for name in _imports(path))
    }
    assert violations == {}


def test_evaluation_does_not_access_private_agent_state() -> None:
    root = Path(__file__).resolve().parents[1] / "agents" / "evaluation"
    violations = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_") and isinstance(node.value, ast.Name) and node.value.id == "agent":
                violations.append(f"{path}:{node.lineno}:{node.attr}")
    assert violations == []


@pytest.mark.asyncio
async def test_model_request_trace_is_bound_to_context_snapshot(tmp_path: Path) -> None:
    class Provider:
        def __init__(self) -> None:
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            return ModelResponse(text="done", stop_reason="stop", usage={"input": 3, "output": 1})

    provider = Provider()
    result = await AgentHarness().run(TaskSpec(
        "trace-context",
        "inspect context",
        tmp_path,
        mode="interactive",
        budget=BudgetSpec(total_turns=1, solve_turns=1, repair_turns=0, max_repair_attempts=0),
        runtime=RuntimeConfig(
            provider=ProviderSettings(adapter=provider),
            session=SessionSettings(trace_root=tmp_path / "traces"),
            extensions=ExtensionSettings(
                disabled=frozenset(
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
            ),
        ),
    ))

    events = load_trace(result.trace_path)
    request = next(event for event in events if event["type"] == "model.request")
    digest = request["payload"]["context_digest"]
    assert len(digest) == 64
    assert request["payload"]["message_count"] == 1
    expected = hashlib.sha256(json.dumps(
        tuple(provider.requests[0].messages), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")).hexdigest()
    assert digest == expected
