from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.evolution.skills import _parse_skill_file
from agents.extensions import (
    EXTENSION_API_VERSION,
    ExtensionHost,
    ExtensionLoadError,
    ExtensionSpec,
)
from agents.extensions.defaults import default_extension_specs
from agents.extensions.loader import ExtensionDiscoveryError, load_extension_spec
from agents.extensions.policy import _approval_allows_exit
from agents.harness import BudgetExceeded, BudgetLedger, BudgetSpec
from agents.runtime.contracts import ToolCall
from agents.runtime.hooks import ToolCallDecision


def test_extension_setup_is_atomic_after_failure() -> None:
    def broken(api) -> None:
        api.register_command("partial", lambda _args, _context: "bad")
        raise RuntimeError("setup failed")

    host = ExtensionHost((ExtensionSpec("broken", broken),))
    with pytest.raises(ExtensionLoadError, match="failed to load extension broken"):
        host.load()
    assert host.commands() == ()


def test_extension_dependency_errors_are_fail_closed() -> None:
    with pytest.raises(ExtensionLoadError, match="missing extension dependencies"):
        ExtensionHost(
            (
                ExtensionSpec(
                    "dependent",
                    lambda _api: None,
                    requires=("missing",),
                ),
            )
        )


def test_default_disable_is_dependency_closed() -> None:
    names = {spec.name for spec in default_extension_specs({"permissions"})}
    assert "permissions" not in names
    assert "plan" not in names
    assert "memory" not in names
    assert "subagents" not in names
    assert "skills" not in names
    assert "mcp" not in names
    assert "execution" in names
    assert "context" in names


def test_deferred_search_cannot_cross_context_ceiling() -> None:
    def setup(api) -> None:
        for name in ("safe_deferred", "unsafe_deferred"):
            api.register_tool(
                {
                    "name": name,
                    "description": name,
                    "input_schema": {"type": "object"},
                },
                lambda _value, _context: "ok",
                deferred=True,
            )

    host = ExtensionHost((ExtensionSpec("deferred", setup),))
    host.load()
    context = SimpleNamespace(
        active_tool_names=frozenset(),
        tool_ceiling_names=frozenset({"safe_deferred"}),
    )
    assert host.search_tools("unsafe_deferred", context) == []
    assert context.active_tool_names == frozenset()
    assert host.search_tools("safe_deferred", context)[0]["name"] == "safe_deferred"
    assert context.active_tool_names == frozenset({"safe_deferred"})


@pytest.mark.asyncio
async def test_mutated_tool_input_reaches_final_authorizer() -> None:
    seen: list[dict[str, str]] = []

    def setup(api) -> None:
        api.register_tool(
            {
                "name": "command_tool",
                "description": "command",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
            lambda _value, _context: "ok",
        )

        async def mutate(event, _context) -> None:
            call = event.data["call"]
            event.data["call"] = ToolCall(
                call.id,
                call.name,
                {"command": "mutated"},
            )

        api.on("tool_call", mutate)

    async def authorize(_name, value, _context, _call_id):
        seen.append(dict(value))
        return ToolCallDecision()

    host = ExtensionHost((ExtensionSpec("mutation", setup),))
    host.load()
    context = SimpleNamespace(
        active_tool_names=None,
        tool_ceiling_names=None,
        services={"authorizer": authorize},
    )
    with host.use_context(context):
        decision = await host.before_tool_call(
            ToolCall("call-1", "command_tool", {"command": "original"})
        )
    assert decision.action == "allow"
    assert decision.input == {"command": "mutated"}
    assert seen == [{"command": "mutated"}]


def test_extension_loader_registers_module_before_execution(tmp_path: Path) -> None:
    module = tmp_path / "extension.py"
    module.write_text(
        "\n".join(
            [
                "import sys",
                "if __name__ not in sys.modules:",
                "    raise RuntimeError('module not registered')",
                f"EXTENSION_API_VERSION = {EXTENSION_API_VERSION}",
                "EXTENSION_NAME = 'loaded'",
                "def setup(api):",
                "    api.register_command('loaded', lambda args, context: args)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    spec = load_extension_spec(module, scope="explicit")
    assert spec.name == "loaded"


def test_extension_loader_rejects_incompatible_api(tmp_path: Path) -> None:
    module = tmp_path / "extension.py"
    module.write_text(
        "EXTENSION_API_VERSION = 999\ndef setup(api):\n    pass\n",
        encoding="utf-8",
    )
    with pytest.raises(ExtensionDiscoveryError, match="incompatible"):
        load_extension_spec(module, scope="explicit")


def test_json_looking_malformed_skill_allowlist_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: unsafe\nallowed-tools: [run_shell]\n---\nDo work.\n",
        encoding="utf-8",
    )
    skill = _parse_skill_file(path, "project", str(tmp_path))
    assert skill is not None
    assert skill.allowed_tools == []


def test_plan_exit_only_accepts_literal_true_or_exact_approve_choice() -> None:
    assert _approval_allows_exit(True)
    assert _approval_allows_exit({"choice": "approve"})
    for value in (
        False,
        "approve",
        "reject",
        1,
        {"choice": "reject"},
        {"choice": "execute"},
        {"choice": "manual-execute"},
        object(),
    ):
        assert not _approval_allows_exit(value)


def test_budget_exhaustion_is_latched() -> None:
    ledger = BudgetLedger(
        BudgetSpec(
            total_turns=1,
            solve_turns=1,
            repair_turns=0,
            max_repair_attempts=0,
            max_input_tokens=1,
        )
    )
    with pytest.raises(BudgetExceeded, match="input token"):
        ledger.consume_usage(input_tokens=2)
    with pytest.raises(BudgetExceeded, match="input token"):
        ledger.ensure_available()
    with pytest.raises(BudgetExceeded, match="input token"):
        ledger.consume_turn()
