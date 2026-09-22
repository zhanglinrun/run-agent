"""Host defaults load memory and verifier-gated evolution without changing snapshots."""

from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import ReplyProvider, options
from typer.testing import CliRunner

from run_agent_coding import cli
from run_agent_coding.application import CodingApplication
from run_agent_core.messages import ToolCall
from run_agent_extensions import BUILTIN_EXTENSIONS


@pytest.mark.parametrize(
    "enabled,explicit", [(True, False), (True, True), (False, False), (False, True)]
)
async def test_default_experience_and_explicit_paths(tmp_path, enabled, explicit):
    opts = replace(
        options(tmp_path),
        extensions_enabled=enabled,
        trust_override="decline",
        extension_paths=(BUILTIN_EXTENSIONS["experience"],) if explicit else (),
    )
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        names = [tool.name for tool in app.session.tools]
        assert names.count("memory") == int(enabled or explicit)
        assert names.count("skill_manage") == int(enabled or explicit)
        sources = app.session.extension_runtime.source_manifest()
        expected = tuple(BUILTIN_EXTENSIONS.values()) if enabled else opts.extension_paths
        assert {item["source_id"] for item in sources} == {
            f"extension:{(path / 'extension.py').as_uri()}" for path in expected
        }
        assert len(sources) == len(expected)
        if enabled:
            assert (await app.command("/plan status")).message == "Plan mode is off."
        if enabled or explicit:
            # The shipped command surface is what the extension registers, and nothing else:
            # every real command is handled, an unknown name stays unhandled.
            for command in ("/evolve status", "/memory show"):
                assert (await app.command(command)).handled is True
            assert "evolution evaluation:" in (await app.command("/evolve status")).message
            assert (await app.command("/nosuchcommand status")).handled is False
            memory = next(tool for tool in app.session.tools if tool.name == "memory")
            with pytest.raises(ValueError, match="project inputs are untrusted"):
                await memory.execute(
                    "untrusted", {"target": "memory", "action": "add", "content": "Uses pytest"}
                )
            assert not (tmp_path / ".run" / "MEMORY.md").exists()


@pytest.mark.parametrize("original_enabled", [False, True])
async def test_resume_preserves_snapshot_and_refresh_adopts_default(tmp_path, original_enabled):
    # Simulate historical sessions with zero extensions or experience alone.
    opts = replace(
        options(tmp_path),
        extensions_enabled=False,
        extension_paths=(BUILTIN_EXTENSIONS["experience"],) if original_enabled else (),
    )
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        session_id = app.session.session_id
        _ = [event async for event in app.prompt("hello")]
    resumed = replace(opts, extensions_enabled=True, extension_paths=(), resume=session_id)
    async with await CodingApplication.open(resumed, provider=ReplyProvider()) as app:
        assert ("memory" in {tool.name for tool in app.session.tools}) is original_enabled
        assert len(app.session.extension_runtime.source_manifest()) == int(original_enabled)
        assert any(message.text == "hello" for message in app.session.messages)
    async with await CodingApplication.open(
        replace(resumed, refresh_resources=True), provider=ReplyProvider()
    ) as app:
        await app.start()
        assert "memory" in {tool.name for tool in app.session.tools}
        assert len(app.session.extension_runtime.source_manifest()) == len(BUILTIN_EXTENSIONS)
        assert any(message.text == "hello" for message in app.session.messages)


@pytest.mark.parametrize("disabled", [False, True])
def test_cli_passes_default_and_opt_out_to_application(tmp_path, monkeypatch, disabled):
    observed = []

    async def run(opts, *args, **kwargs):
        async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
            observed.append({tool.name for tool in app.session.tools})
        return True

    monkeypatch.setattr(cli, "_run", run)
    args = ["--cwd", str(tmp_path), "--state-dir", str(tmp_path / "state"), "--print", "hello"]
    if disabled:
        args.append("--no-extensions")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert ("memory" in observed[0]) is not disabled


async def test_default_plan_and_experience_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("RUN_AGENT_PERMISSION_MODE", raising=False)
    async with await CodingApplication.open(
        replace(options(tmp_path), extensions_enabled=True), provider=ReplyProvider()
    ) as app:
        await app.start()
        runtime = app.session.extension_runtime

        async def blocked(name, arguments):
            return (
                await runtime.before_tool_call(ToolCall(id="probe", name=name, arguments=arguments))
            ).block

        # Without plan mode the ordinary foreground tools retain their normal policy.
        assert not await blocked("write", {"path": "inside.txt", "content": "ok"})
        assert not await blocked("write", {"path": str(tmp_path.parent / "outside.txt")})
        assert not await blocked("bash", {"command": "git reset --hard"})
        assert not await blocked("bash", {"command": "git status"})
        await app.command("/plan on")
        for name, arguments in (
            ("memory", {"action": "add", "content": "fact"}),
            ("memory", {"action": "batch", "operations": []}),
            ("skill_manage", {"action": "propose"}),
            ("write", {"path": "inside.txt"}),
        ):
            assert await blocked(name, arguments)
        for action in ("list", "view"):
            assert not await blocked("skill_manage", {"action": action})
        assert not await blocked("read", {"path": "inside.txt"})
        await app.command("/plan off")
        assert not await blocked("skill_manage", {"action": "propose"})
