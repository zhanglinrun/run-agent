"""Extension lifecycle acceptance.

- A setup that fails halfway must leave no tool, command or prompt guideline.
- With every extension disabled, skills and recovery still work; compression is
  gone with the extensions, leaving only the hard window guard.
- Extension shutdown reports concrete convergence values and resources; the
  task budget half of that is covered in test_task_budget.
"""

from dataclasses import replace

from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_core.messages import UserMessage

HALF_SETUP = """
from run_agent_core.tools import AgentTool, AgentToolResult


def setup(api):
    async def run(*args, **kwargs):
        return AgentToolResult(content=[])

    api.register_tool(
        AgentTool(
            name="half",
            label="Half",
            description="Registered before setup fails.",
            parameters={"type": "object"},
            execute_fn=run,
            execution_mode="sequential",
        )
    )
    api.register_command("half", lambda arguments: "ok")
    api.add_prompt_guideline("A guideline registered before setup fails.")
    raise ValueError("setup failure")
"""

OWNED_SETUP = """
from pathlib import Path


def setup(api):
    async def cleanup():
        Path(__file__).with_suffix(".closed").touch()

    async def work(payload, task):
        return {"done": True}

    api.register_disposer(cleanup)
    api.register_task_handler("owned-work", work)
"""


async def test_half_failed_setup_leaves_no_tool_command_or_guideline(tmp_path):
    extension = tmp_path / "half.py"
    extension.write_text(HALF_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        runtime = app.session.extension_runtime
        assert runtime.extension_tools == ()
        assert runtime.prompt_guidelines == ()
        assert runtime.build_command_registry().get("half") is None
        assert runtime.build_command_registry().get("session") is not None


async def test_coding_skills_and_recovery_survive_without_extensions(tmp_path):
    opts = options(tmp_path)
    assert opts.extensions_enabled is False
    app = await CodingApplication.open(opts, provider=ReplyProvider())
    await app.start()
    runtime = app.session.extension_runtime
    assert runtime.extension_tools == ()
    assert runtime.build_command_registry().get("memory") is None

    events = [event async for event in app.prompt("first")]
    head = events[-1].head_id
    session_id = app.session.session_id

    assert (await app.command("/skills")).handled
    assert (await app.command(f"/branch {head}")).handled
    await app.aclose()

    resumed = await CodingApplication.open(
        replace(opts, resume=session_id), provider=ReplyProvider()
    )
    try:
        texts = [
            message.text for message in resumed.session.messages if isinstance(message, UserMessage)
        ]
        assert "first" in texts
    finally:
        await resumed.aclose()


async def test_extension_shutdown_reports_concrete_convergence(tmp_path):
    extension = tmp_path / "owned.py"
    extension.write_text(OWNED_SETUP, encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(extension,))
    app = await CodingApplication.open(opts, provider=ReplyProvider())
    await app.start()
    assert not extension.with_suffix(".closed").exists()

    result = await app.session.extension_runtime.aclose()

    assert result.drained is True
    assert result.contained_managed_tasks == 0
    assert result.contained_disposers == 0
    assert result.contained_discovery_tasks == 0
    assert result.cleanup_errors == ()
    assert extension.with_suffix(".closed").exists()
    await app.aclose()
