"""Host defaults: experience, memory and compaction load without changing snapshots."""

import json
from dataclasses import replace

import pytest
from tests.redesign.test_coding_application import RecordingProvider, ReplyProvider, options
from typer.testing import CliRunner

from run_agent_coding import cli
from run_agent_coding.application import CodingApplication
from run_agent_core.messages import ToolCall, UserMessage
from run_agent_extensions import BUILTIN_EXTENSIONS


@pytest.mark.parametrize(
    "enabled,explicit", [(True, False), (True, True), (False, False), (False, True)]
)
async def test_default_extensions_and_explicit_paths(tmp_path, enabled, explicit):
    opts = replace(
        options(tmp_path),
        extensions_enabled=enabled,
        trust_override="decline",
        extension_paths=(BUILTIN_EXTENSIONS["experience"],) if explicit else (),
    )
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        names = [tool.name for tool in app.session.tools]
        # ``memory`` belongs to the memory built-in only: loading experience alone
        # must not bring a memory tool or a /memory command back.
        assert names.count("memory") == int(enabled)
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
            assert (await app.command("/evolve status")).handled is True
            assert "evolution evaluation:" in (await app.command("/evolve status")).message
            assert (await app.command("/nosuchcommand status")).handled is False
        if enabled:
            for command in ("/memory show", "/force-snip", "/four-layer-compact"):
                assert (await app.command(command)).handled is True
        elif explicit:
            assert (await app.command("/memory show")).handled is False
        if enabled:
            memory = next(tool for tool in app.session.tools if tool.name == "memory")
            refused = await memory.execute(
                "untrusted", {"target": "memory", "action": "add", "content": "Uses pytest"}
            )
            assert refused.details["accepted"] is False
            assert "project inputs are untrusted" in refused.text
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
        names = {tool.name for tool in app.session.tools}
        # The saved snapshot keeps the old surface: a pre-split experience session
        # has Skills, but memory stays where the snapshot put it (nowhere).
        assert "memory" not in names
        assert ("skill_manage" in names) is original_enabled
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


@pytest.mark.parametrize("name", ["compaction", "experience", "memory"])
def test_cli_resolves_the_builtin_short_names(tmp_path, monkeypatch, name):
    seen = {}

    async def run(opts, *args, **kwargs):
        seen["paths"] = opts.extension_paths
        return True

    monkeypatch.setattr(cli, "_run", run)
    result = CliRunner().invoke(
        cli.app,
        [
            "--cwd",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--no-extensions",
            "--extension",
            name,
            "--print",
            "hello",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["paths"] == (BUILTIN_EXTENSIONS[name],)


async def test_default_run_serves_memory_from_the_memory_extension(tmp_path):
    """The acceptance end to end: the default set really delivers memory."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "MEMORY.md").write_text("Uses pytest-xdist", encoding="utf-8")
    provider = RecordingProvider()
    async with await CodingApplication.open(
        replace(options(tmp_path), extensions_enabled=True), provider=provider
    ) as app:
        await app.start()
        source_ids = {item["source_id"] for item in app.session.extension_runtime.source_manifest()}
        assert f"extension:{(BUILTIN_EXTENSIONS['memory'] / 'extension.py').as_uri()}" in source_ids
        assert "memory" in {tool.name for tool in app.session.tools}

        shown = await app.command("/memory show")
        assert shown.handled is True
        assert "MEMORY.md" in shown.message and "Uses pytest-xdist" in shown.message

        events = [event async for event in app.prompt("which test runner does this project use?")]
        assert events[-1].status == "succeeded"
        system = provider.requests[-1]["system"]
        assert "# Long-term memory" in system and "Uses pytest-xdist" in system

        # The memory layer is prompt-only: the durable transcript keeps the clean
        # conversation, and no request-local recall fence is persisted.
        assert [
            message.text for message in app.session.messages if isinstance(message, UserMessage)
        ] == ["which test runner does this project use?"]
        assert all(
            "<memory-context>" not in getattr(message, "text", "")
            for message in app.session.messages
        )
        session_file = tmp_path / ".run" / "sessions" / f"{app.session.session_id}.jsonl"
        assert session_file.is_file()
        rows = [
            json.loads(line)
            for line in session_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        message_entries = [row["message"] for row in rows if row.get("type") == "message"]
        assert [entry["role"] for entry in message_entries] == ["user", "assistant"]
        assert all(
            "<memory-context>" not in json.dumps(entry, ensure_ascii=False)
            and "Uses pytest-xdist" not in json.dumps(entry, ensure_ascii=False)
            and "MEMORY (your personal notes)" not in json.dumps(entry, ensure_ascii=False)
            for entry in message_entries
        )
