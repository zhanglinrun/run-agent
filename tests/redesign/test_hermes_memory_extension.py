"""``setup(api)`` wiring: hooks, tool, command and the default-load smoke test."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from run_agent_coding.extensions import (
    BeforeAgentStartEvent,
    ContextEvent,
    ExtensionCommandContext,
    InputEvent,
    SessionBeforeCompactEvent,
    SessionBeforeCompactResult,
    SessionStartEvent,
    TurnStartEvent,
)
from run_agent_coding.paths import RunAgentPaths
from run_agent_core.messages import UserMessage
from run_agent_core.tools import AgentTool
from run_agent_extensions.hermes_memory import (
    MEMORY_USAGE,
    PROMPT_GUIDELINE,
    HermesMemoryConfig,
    MemoryCall,
    inject_recall_block,
    load_hermes_memory_config,
    resolve_stores,
    setup,
)
from run_agent_extensions.hermes_memory.manager import MemoryManager

EXTENSION_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "run_agent_extensions" / "hermes_memory"
)

EXPECTED_HOOKS = {
    "session_start",
    "before_agent_start",
    "input",
    "context",
    "turn_start",
    "agent_settled",
    "session_before_compact",
    "session_before_switch",
    "session_shutdown",
}


class StubUi:
    """UI bridge stub; ``has_ui`` off means dialogs answer with the no-op default."""

    def __init__(self, *, has_ui: bool = False, confirm: bool = False) -> None:
        self.has_ui = has_ui
        self._confirm = confirm
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> None:
        self.notifications.append((message, level))

    def set_status(self, source: str, key: str, text: str | None) -> None:
        return None

    def clear_status(self, source: str | None = None) -> None:
        return None

    async def select(
        self, title: str, options: object, *, timeout: float | None = None
    ) -> str | None:
        return None

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        return self._confirm

    async def input(
        self, title: str, placeholder: str = "", *, timeout: float | None = None
    ) -> str | None:
        return None


class StubContext:
    """The slice of ``ExtensionContext`` this extension actually reads."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        environment: dict[str, str] | None = None,
        project_resources_enabled: bool = True,
        session_id: str = "session-1",
        ui: StubUi | None = None,
    ) -> None:
        self.paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")
        self.cwd = tmp_path
        self.environment = environment or {}
        self.project_resources_enabled = project_resources_enabled
        self.session_id = session_id
        self.transcript: tuple[object, ...] = ()
        self.ui = ui or StubUi()
        self.has_ui = self.ui.has_ui


class StubApi:
    """Registers everything ``setup(api)`` hands over, for direct invocation."""

    def __init__(self, context: StubContext) -> None:
        self.context = context
        self.tools: dict[str, AgentTool] = {}
        self.commands: dict[str, object] = {}
        self.handlers: dict[str, list[object]] = {}
        self.guidelines: list[str] = []

    def register_tool(self, tool: AgentTool) -> None:
        self.tools[tool.name] = tool

    def register_command(
        self,
        name: str,
        handler: object,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self.commands[name] = handler

    def on(self, event: str, handler: object | None = None) -> object:
        if handler is None:
            return lambda decorated: decorated
        self.handlers.setdefault(event, []).append(handler)
        return handler

    def add_prompt_guideline(self, guideline: str) -> None:
        self.guidelines.append(guideline)


async def started(
    tmp_path: Path,
    *,
    environment: dict[str, str] | None = None,
    project_resources_enabled: bool = True,
    ui: StubUi | None = None,
) -> tuple[StubApi, StubContext]:
    context = StubContext(
        tmp_path,
        environment=environment,
        project_resources_enabled=project_resources_enabled,
        ui=ui,
    )
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]
    await fire(api, "session_start", SessionStartEvent(reason="startup"), context)
    return api, context


async def fire(api: StubApi, event: str, payload: object, context: StubContext) -> object:
    result: object = None
    for handler in api.handlers[event]:
        result = await handler(payload, context)  # type: ignore[operator]
    return result


async def command(api: StubApi, args: str, context: StubContext) -> str | None:
    handler = api.commands["memory"]
    return await handler(args, ExtensionCommandContext(name="memory", args=args, api=api))  # type: ignore[operator]


def test_setup_registers_the_hook_tool_and_command_surface(tmp_path: Path) -> None:
    context = StubContext(tmp_path)
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]

    assert set(api.handlers) == EXPECTED_HOOKS
    assert set(api.tools) == {"memory"}
    assert set(api.commands) == {"memory"}
    assert api.guidelines == [PROMPT_GUIDELINE]
    assert "reference data" in PROMPT_GUIDELINE
    assert api.tools["memory"].execution_mode == "sequential"


def test_tool_schema_pins_the_documented_call_surface() -> None:
    # The one memory call surface: target/action/content/old_text/new_content/new_text/
    # operations[{action,content,old_text,new_content,new_text}]/scope, strict extras.
    schema = MemoryCall.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "target",
        "action",
        "content",
        "old_text",
        "new_content",
        "new_text",
        "operations",
        "scope",
    }
    operation = schema["$defs"]["MemoryOperation"]
    assert operation["additionalProperties"] is False
    assert set(operation["properties"]) == {
        "action",
        "content",
        "old_text",
        "new_content",
        "new_text",
    }
    assert schema["properties"]["target"]["default"] == "memory"
    assert schema["properties"]["action"]["anyOf"][0]["enum"] == [
        "add",
        "replace",
        "remove",
        "batch",
    ]


@pytest.mark.parametrize(
    "environment,expected",
    [
        ({}, HermesMemoryConfig()),
        (
            {
                "HERMES_MEMORY_CHAR_LIMIT": "500",
                "HERMES_MEMORY_USER_CHAR_LIMIT": "400",
                "HERMES_MEMORY_ENABLED": "no",
                "HERMES_MEMORY_USER_PROFILE_ENABLED": "yes",
                "HERMES_MEMORY_WRITE_APPROVAL": "on",
                "HERMES_MEMORY_PREFETCH_TIMEOUT": "2.5",
                "HERMES_MEMORY_DRAIN_TIMEOUT": "1.5",
            },
            HermesMemoryConfig(
                memory_char_limit=500,
                user_char_limit=400,
                memory_enabled=False,
                user_profile_enabled=True,
                write_approval=True,
                prefetch_timeout=2.5,
                drain_timeout=1.5,
            ),
        ),
    ],
)
def test_config_loader(environment: dict[str, str], expected: HermesMemoryConfig) -> None:
    assert load_hermes_memory_config(environment) == expected


@pytest.mark.parametrize(
    "environment",
    [
        {"HERMES_MEMORY_CHAR_LIMIT": "10"},
        {"HERMES_MEMORY_ENABLED": "maybe"},
        {"HERMES_MEMORY_DRAIN_TIMEOUT": "0"},
    ],
)
def test_config_loader_rejects_invalid_values(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        load_hermes_memory_config(environment)


def test_resolve_stores_maps_the_user_and_project_scopes(tmp_path: Path) -> None:
    paths = RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")
    stores = resolve_stores(paths, tmp_path, config=HermesMemoryConfig())

    assert stores["user"].file("memory").path == paths.home / "MEMORY.md"
    assert stores["project"].file("memory").path == tmp_path / ".run" / "MEMORY.md"


async def test_before_agent_start_appends_a_byte_stable_snapshot_section(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "MEMORY.md").write_text("Runs pytest-xdist", encoding="utf-8")
    api, context = await started(tmp_path)

    event = BeforeAgentStartEvent(prompt="hello", system_prompt="BASE")
    first = await fire(api, "before_agent_start", event, context)
    second = await fire(api, "before_agent_start", event, context)

    assert first is not None and second is not None
    assert first.system_prompt == second.system_prompt
    assert "# Long-term memory" in first.system_prompt
    assert "Runs pytest-xdist" in first.system_prompt
    assert first.system_prompt.startswith("BASE\n\n# Long-term memory")

    # A write during the session is durable but does not move the prompt prefix.
    tool = api.tools["memory"]
    _ = await tool.execute("call-1", {"target": "user", "action": "add", "content": "Likes TDD"})
    third = await fire(api, "before_agent_start", event, context)
    assert third.system_prompt == first.system_prompt


def test_before_agent_start_is_a_noop_without_a_snapshot(tmp_path: Path) -> None:
    context = StubContext(tmp_path)
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]
    result = api.handlers["before_agent_start"]
    assert result == [] or result


async def test_context_hook_returns_nothing_without_recall(tmp_path: Path) -> None:
    api, context = await started(tmp_path)
    messages = (UserMessage(content="hi"),)

    assert await fire(api, "context", ContextEvent(messages=messages), context) is None


async def test_input_hook_tracks_the_turn_without_touching_history(tmp_path: Path) -> None:
    api, context = await started(tmp_path)

    assert await fire(api, "input", InputEvent(text="hi"), context) is None
    assert await fire(api, "turn_start", TurnStartEvent(turn_index=0, timestamp=1), context) is None
    messages = (UserMessage(content="hi"),)
    assert await fire(api, "context", ContextEvent(messages=messages), context) is None
    assert [message.text for message in messages] == ["hi"]


def test_inject_recall_block_is_request_local() -> None:
    messages: tuple[object, ...] = (UserMessage(content="what do I prefer?"),)
    block = "<memory-context>\n[System note: recalled memory]\n\nprefers pytest\n</memory-context>"

    injected = inject_recall_block(messages, block)  # type: ignore[arg-type]

    assert len(injected) == 2
    assert isinstance(injected[-1], UserMessage)
    assert injected[-1].text == block  # type: ignore[union-attr]
    # The durable list is untouched: the hook only ever changes the request copy.
    assert len(messages) == 1
    assert inject_recall_block(messages, "  ") == tuple(messages)  # type: ignore[arg-type]


async def test_memory_tool_happy_path_and_duplicate(tmp_path: Path) -> None:
    api, _ = await started(tmp_path)
    tool = api.tools["memory"]

    added = await tool.execute(
        "call-1", {"target": "user", "action": "add", "content": "Prefers pytest"}
    )
    assert added.details["accepted"] is True
    assert added.details["scope"] == "user"
    assert added.details["target"] == "user"
    assert added.details["done"] is True
    assert "Write saved" in added.content[0].text
    assert "Prefers pytest" in (tmp_path / "home" / "USER.md").read_text(encoding="utf-8")

    duplicate = await tool.execute(
        "call-2", {"target": "user", "action": "add", "content": "Prefers pytest"}
    )
    assert duplicate.details["accepted"] is True
    assert "already exists" in duplicate.content[0].text

    replaced = await tool.execute(
        "call-3",
        {
            "target": "user",
            "action": "replace",
            "old_text": "pytest",
            "new_content": "Prefers ruff",
        },
    )
    assert replaced.details["accepted"] is True

    removed = await tool.execute(
        "call-4", {"target": "user", "action": "remove", "old_text": "ruff"}
    )
    assert removed.details["accepted"] is True
    assert (tmp_path / "home" / "USER.md").read_text(encoding="utf-8") == ""


async def test_memory_tool_refusal_paths(tmp_path: Path) -> None:
    api, _ = await started(tmp_path)
    tool = api.tools["memory"]

    missing_action = await tool.execute("call-1", {"target": "user"})
    assert missing_action.details["accepted"] is False
    assert "action must be add, replace, remove or batch" in missing_action.content[0].text

    with pytest.raises(ValidationError):
        await tool.execute("call-2", {"target": "user", "action": "add", "unexpected": "x"})

    with pytest.raises(ValidationError):
        await tool.execute(
            "call-3",
            {"target": "user", "action": "add", "content": "x", "scope": "elsewhere"},
        )

    untrusted_api, _ = await started(tmp_path / "untrusted", project_resources_enabled=False)
    project_write = await untrusted_api.tools["memory"].execute(
        "call-4", {"target": "memory", "action": "add", "content": "project fact"}
    )
    assert project_write.details["accepted"] is False
    assert "project inputs are untrusted" in project_write.content[0].text
    assert not (tmp_path / "untrusted" / ".run").exists()


async def test_memory_tool_requires_a_started_session(tmp_path: Path) -> None:
    context = StubContext(tmp_path)
    api = StubApi(context)
    setup(api)  # type: ignore[arg-type]

    result = await api.tools["memory"].execute(
        "call-1", {"target": "user", "action": "add", "content": "x"}
    )
    assert result.details["accepted"] is False
    assert "available after session start" in result.content[0].text


async def test_write_approval_is_required_before_any_file_changes(tmp_path: Path) -> None:
    environment = {"HERMES_MEMORY_WRITE_APPROVAL": "true"}
    denied_api, _ = await started(tmp_path / "denied", environment=environment)
    denied = await denied_api.tools["memory"].execute(
        "call-1", {"target": "user", "action": "add", "content": "x"}
    )
    assert denied.details["accepted"] is False
    assert "not approved" in denied.content[0].text
    assert not (tmp_path / "denied" / "home" / "USER.md").exists()

    approving = StubUi(has_ui=True, confirm=True)
    allowed_api, _ = await started(tmp_path / "allowed", environment=environment, ui=approving)
    allowed = await allowed_api.tools["memory"].execute(
        "call-2", {"target": "user", "action": "add", "content": "x"}
    )
    assert allowed.details["accepted"] is True
    assert "x" in (tmp_path / "allowed" / "home" / "USER.md").read_text(encoding="utf-8")


async def test_command_show_add_replace_remove(tmp_path: Path) -> None:
    api, context = await started(tmp_path)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "MEMORY.md").write_text("Runs pytest", encoding="utf-8")

    shown = await command(api, "show", context)
    assert shown is not None
    assert "MEMORY.md" in shown

    usage = await command(api, "", context)
    assert usage == MEMORY_USAGE

    added = await command(api, "add user Prefers pytest", context)
    assert added is not None and "Added in USER.md" in added
    assert "Prefers pytest" in await command_or_empty(api, "show", context)

    replaced = await command(api, "replace user pytest ruff", context)
    assert replaced is not None and "Replaced in USER.md" in replaced

    removed = await command(api, "remove user ruff", context)
    assert removed is not None and "Removed in USER.md" in removed

    scoped = await command(api, "add user Project fact --scope project", context)
    assert scoped is not None and "Added in USER.md" in scoped

    with pytest.raises(ValueError, match="--scope needs project or user"):
        await command(api, "show --scope nope", context)

    assert await command(api, "nonsense", context) == MEMORY_USAGE


async def command_or_empty(api: StubApi, args: str, context: StubContext) -> str:
    return await command(api, args, context) or ""


async def test_mirrored_writes_are_reported_to_external_providers(tmp_path: Path) -> None:
    api, _ = await started(tmp_path)

    # The builtin provider is the writer, so the manager skips it; a batch expands
    # into its individual operations for the (absent) external providers.
    added = await api.tools["memory"].execute(
        "call-1", {"target": "user", "action": "add", "content": "Prefers pytest"}
    )
    assert added.details["accepted"] is True
    batched = await api.tools["memory"].execute(
        "call-2",
        {
            "target": "user",
            "action": "batch",
            "operations": [
                {"action": "add", "content": "Likes TDD"},
                {"action": "remove", "old_text": "Prefers pytest"},
            ],
        },
    )
    assert batched.details["accepted"] is True


async def test_extension_loads_under_the_real_runtime(tmp_path: Path) -> None:
    """Smoke test: every hook name this package subscribes to must be real."""
    from tests.redesign.test_coding_application import RecordingProvider, options

    from run_agent_coding.application import CodingApplication

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "MEMORY.md").write_text("Uses pytest-xdist", encoding="utf-8")
    opts = replace(options(tmp_path), extension_paths=(EXTENSION_DIR,))
    provider = RecordingProvider()

    async with await CodingApplication.open(opts, provider=provider) as app:
        await app.start()
        runtime = app.session.extension_runtime
        assert "memory" in {tool.name for tool in app.session.tools}
        assert not [item for item in runtime.diagnostics if "unknown event" in item.message]

        shown = await app.command("/memory show")
        assert shown.handled is True
        assert "MEMORY.md" in shown.message

        events = [event async for event in app.prompt("which test runner does this project use?")]
        assert events
        system = provider.requests[-1]["system"]
        assert "# Long-term memory" in system
        assert "Uses pytest-xdist" in system
        # Recall injection never rewrites the durable transcript.
        assert all(
            "<memory-context>" not in getattr(message, "text", "")
            for message in app.session.messages
        )

        memory = next(tool for tool in app.session.tools if tool.name == "memory")
        written = await memory.execute(
            "call-1", {"target": "user", "action": "add", "content": "Prefers short answers"}
        )
        assert written.details["accepted"] is True
        assert "Prefers short answers" in (tmp_path / "state" / "USER.md").read_text(
            encoding="utf-8"
        )

        _ = [event async for event in app.prompt("thanks")]
        # The snapshot is frozen for the session: the new entry is on disk, not in the
        # prompt, until the next session load.
        assert "Prefers short answers" not in provider.requests[-1]["system"]


async def test_before_compact_returns_the_provider_text_as_gate_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-compression insight is returned, and never carried into memory."""
    monkeypatch.setattr(
        MemoryManager, "on_pre_compress", lambda self, messages: "memory: pytest -q"
    )
    api, context = await started(tmp_path)
    context.transcript = (UserMessage(content="hi"),)

    result = await fire(
        api, "session_before_compact", SessionBeforeCompactEvent(reason="threshold"), context
    )

    assert isinstance(result, SessionBeforeCompactResult)
    assert result.cancel is False
    assert result.context == "memory: pytest -q", (
        "the text travels back to the compaction extension as summarizer material"
    )
    # Memory and session history are separate layers: the next request's memory
    # block is untouched by the compaction that just read the transcript.
    messages = (UserMessage(content="hi"),)
    assert await fire(api, "context", ContextEvent(messages=messages), context) is None
    assert [message.text for message in messages] == ["hi"]


async def test_before_compact_returns_an_empty_context_without_a_contribution(
    tmp_path: Path,
) -> None:
    """A provider with nothing to say contributes nothing at all."""
    api, context = await started(tmp_path)
    context.transcript = (UserMessage(content="hi"),)

    result = await fire(
        api, "session_before_compact", SessionBeforeCompactEvent(reason="threshold"), context
    )

    assert isinstance(result, SessionBeforeCompactResult)
    assert (result.cancel, result.context) == (False, "")
