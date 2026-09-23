from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import ReplyProvider, options

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import ThinkingLevelSelectEvent
from run_agent_coding.extensions.api import (
    EXTENSION_EVENT_TYPES,
    HOOK_EVENT_TYPES,
    OBSERVATION_EVENT_TYPES,
    SessionCompactEvent,
    SessionCompactFailedEvent,
)
from run_agent_coding.extensions.runtime import ExtensionRuntime
from run_agent_coding.resources import RunAgentResourcePaths
from run_agent_core.events import MessageEndEvent
from run_agent_core.messages import AssistantMessage, TextContent, UserMessage
from run_agent_core.provider import ModelRequest


def _runtime(tmp_path: Path, source: str) -> ExtensionRuntime:
    extension = tmp_path / "pi_events.py"
    extension.write_text(source, encoding="utf-8")
    runtime = ExtensionRuntime()
    runtime.load(
        RunAgentResourcePaths(root=tmp_path / "run-home", cwd=tmp_path),
        extra_paths=(extension,),
        include_resource_dirs=False,
        include_user_dir=False,
        include_project_dir=False,
    )
    return runtime


def test_subscribe_accepts_pi_36_and_rejects_old_names(tmp_path):
    names = "\n".join(
        f'    api.on("{name}", lambda event, context: None)'
        for name in sorted(EXTENSION_EVENT_TYPES)
    )
    old = "\n".join(
        f'    api.on("{name}", lambda event, context: None)'
        for name in (
            "queue_update",
            "auto_retry_start",
            "auto_retry_end",
            "entry_appended",
            "compaction_start",
            "compaction_end",
            "thinking_level_changed",
        )
    )
    runtime = _runtime(tmp_path, f"def setup(api):\n{names}\n{old}\n")
    assert len(OBSERVATION_EVENT_TYPES) == 21
    assert len(HOOK_EVENT_TYPES) == 15
    assert len(EXTENSION_EVENT_TYPES) == 36
    assert OBSERVATION_EVENT_TYPES.isdisjoint(HOOK_EVENT_TYPES)
    subscribed = {name for ext in runtime._extensions for name in ext.handlers}
    assert subscribed == EXTENSION_EVENT_TYPES
    messages = [item.message for item in runtime.diagnostics]
    for name in (
        "queue_update",
        "auto_retry_start",
        "auto_retry_end",
        "entry_appended",
        "compaction_start",
        "compaction_end",
        "thinking_level_changed",
    ):
        assert any(f"unknown event `{name}`" in message for message in messages)


async def test_session_before_hooks_cancel(tmp_path):
    runtime = _runtime(
        tmp_path,
        """
from run_agent_coding.extensions.api import (
    SessionBeforeCompactResult,
    SessionBeforeForkResult,
    SessionBeforeSwitchResult,
    SessionBeforeTreeResult,
)
def setup(api):
    api.on("session_before_switch", lambda event, context: SessionBeforeSwitchResult(cancel=True))
    api.on("session_before_fork", lambda event, context: SessionBeforeForkResult(cancel=True))
    api.on("session_before_compact", lambda event, context: SessionBeforeCompactResult(cancel=True))
    api.on("session_before_tree", lambda event, context: SessionBeforeTreeResult(cancel=True))
""",
    )
    assert await runtime.emit_session_before_switch("new")
    assert await runtime.emit_session_before_fork("entry-1")
    assert (await runtime.emit_session_before_compact("manual")).cancelled
    assert await runtime.emit_session_before_tree("entry-1")


async def test_message_end_replaces_same_role(tmp_path):
    runtime = _runtime(
        tmp_path,
        """
from run_agent_coding.extensions.api import MessageEndHookResult
from run_agent_core.messages import AssistantMessage, TextContent
def setup(api):
    def replace_message(event, context):
        return MessageEndHookResult(
            message=AssistantMessage(content=[TextContent(text="replaced")])
        )
    api.on("message_end", replace_message)
""",
    )
    event = MessageEndEvent(message=AssistantMessage(content=[TextContent(text="original")]))
    await runtime.apply_message_end(event)
    assert event.message.text == "replaced"


async def test_message_end_rejects_role_change(tmp_path):
    runtime = _runtime(
        tmp_path,
        """
from run_agent_coding.extensions.api import MessageEndHookResult
from run_agent_core.messages import UserMessage
def setup(api):
    def replace_message(event, context):
        return MessageEndHookResult(message=UserMessage(content="nope"))
    api.on("message_end", replace_message)
""",
    )
    original = AssistantMessage(content=[TextContent(text="keep")])
    event = MessageEndEvent(message=original)
    await runtime.apply_message_end(event)
    assert event.message is original
    assert any("unsupported" in item.message for item in runtime.diagnostics)


async def test_before_provider_request_and_headers(tmp_path):
    runtime = _runtime(
        tmp_path,
        """
from run_agent_core.provider import ModelRequest
def setup(api):
    def rewrite(event, context):
        payload = event.payload
        return ModelRequest("other", "sys", payload.messages, payload.tools, payload.session_id)
    def headers(event, context):
        event.headers["X-Trace"] = "1"
        event.headers["drop-me"] = None
    api.on("before_provider_request", rewrite)
    api.on("before_provider_headers", headers)
""",
    )
    request = ModelRequest("orig", "", (UserMessage(content="hi"),), ())
    replaced = await runtime.apply_before_provider_request(request)
    assert replaced.model == "other"
    assert replaced.system == "sys"
    headers = {"drop-me": "x"}
    await runtime.prepare_provider_headers(headers)
    assert headers == {"X-Trace": "1"}


async def test_user_bash_block_and_rewrite(tmp_path):
    runtime = _runtime(
        tmp_path,
        """
from run_agent_coding.extensions.api import UserBashHookResult
def setup(api):
    def bash(event, context):
        if event.command == "blocked":
            return UserBashHookResult(block=True, reason="no")
        return UserBashHookResult(command="echo rewritten")
    api.on("user_bash", bash)
""",
    )
    blocked = await runtime.run_user_bash("blocked", exclude_from_context=False, cwd=tmp_path)
    assert blocked.block and blocked.reason == "no"
    rewritten = await runtime.run_user_bash("ls", exclude_from_context=True, cwd=tmp_path)
    assert rewritten.command == "echo rewritten"


async def test_resources_discover_paths_are_loaded(tmp_path):
    skill_root = tmp_path / "extra-skills"
    (skill_root / "demo").mkdir(parents=True)
    (skill_root / "demo" / "SKILL.md").write_text(
        "---\ndescription: extra skill\n---\nUse the extra skill.\n",
        encoding="utf-8",
    )
    extension = tmp_path / "discover.py"
    extension.write_text(
        "from run_agent_coding.extensions.api import ResourcesDiscoverResult\n"
        "def setup(api):\n"
        "    def discover(event, context):\n"
        f"        return ResourcesDiscoverResult(skill_paths=({str(skill_root)!r},))\n"
        '    api.on("resources_discover", discover)\n',
        encoding="utf-8",
    )
    opts = replace(options(tmp_path), extension_paths=(extension,))
    async with await CodingApplication.open(opts, provider=ReplyProvider()) as app:
        await app.start()
        assert any(skill.name == "demo" for skill in app.session.skills)


async def test_compact_and_thinking_observation_names(tmp_path):
    log = tmp_path / "events.log"
    runtime = _runtime(
        tmp_path,
        f"""
from pathlib import Path
LOG = Path({str(log)!r})
def setup(api):
    def record(event, context):
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(event.type + "\\n")
    api.on("session_compact", record)
    api.on("session_compact_failed", record)
    api.on("thinking_level_select", record)
""",
    )
    await runtime.emit_event(SessionCompactEvent(reason="overflow"))
    await runtime.emit_event(SessionCompactFailedEvent(reason="overflow"))
    await runtime.emit_event(ThinkingLevelSelectEvent(level="off", previous_level="low"))
    assert log.read_text(encoding="utf-8").splitlines() == [
        "session_compact",
        "session_compact_failed",
        "thinking_level_select",
    ]


async def test_ui_prompt_emits_start_and_end(tmp_path):
    log = tmp_path / "ui.log"
    runtime = _runtime(
        tmp_path,
        f"""
from pathlib import Path
LOG = Path({str(log)!r})
def setup(api):
    def record(event, context):
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"{{event.type}}:{{event.kind}}\\n")
    api.on("ui_prompt_start", record)
    api.on("ui_prompt_end", record)
""",
    )
    result = await runtime._extensions[0].api.context.ui.select("Pick", ["a", "b"])
    assert result is None
    assert log.read_text(encoding="utf-8").splitlines() == [
        "ui_prompt_start:select",
        "ui_prompt_end:select",
    ]


async def test_unknown_before_compact_reason_is_tolerated(tmp_path):
    """A non-literal trigger is a diagnostic, never a lost request rewrite."""
    log = tmp_path / "gate.log"
    runtime = _runtime(
        tmp_path,
        f"""
from pathlib import Path
from run_agent_core.provider import ModelRequest
LOG = Path({str(log)!r})
def setup(api):
    def gate(event, context):
        LOG.write_text(event.reason, encoding="utf-8")
        return None
    async def rewrite(event, context):
        await context.request_session_before_compact(reason="weird")
        payload = event.payload
        return ModelRequest(
            "rewritten", payload.system, payload.messages, payload.tools, payload.session_id
        )
    api.on("session_before_compact", gate)
    api.on("before_provider_request", rewrite)
""",
    )
    request = ModelRequest("orig", "", (UserMessage(content="hi"),), ())

    replaced = await runtime.apply_before_provider_request(request)

    assert replaced.model == "rewritten", "the unknown trigger must not take the rewrite down"
    assert log.read_text(encoding="utf-8") == "weird", "the hook observes the raw reason"
    assert any("unknown compaction trigger" in item.message for item in runtime.diagnostics), (
        "the host records the tolerance instead of raising"
    )


async def test_the_compact_gate_merges_the_handlers_context(tmp_path):
    """Every non-empty contribution is merged, blank-line separated, in order."""
    runtime = _runtime(
        tmp_path,
        """
from run_agent_coding.extensions.api import SessionBeforeCompactResult


def setup(api):
    def first(event, context):
        return SessionBeforeCompactResult(context="memory: the user runs pytest -q")

    def broken(event, context):
        raise RuntimeError("this provider is broken")

    def blank(event, context):
        return SessionBeforeCompactResult(context="   \\n  ")

    def fourth(event, context):
        return SessionBeforeCompactResult(context="memory: prefers short answers")

    api.on("session_before_compact", first)
    api.on("session_before_compact", broken)
    api.on("session_before_compact", blank)
    api.on("session_before_compact", fourth)
""",
    )

    decision = await runtime.emit_session_before_compact("manual")

    assert decision.cancelled is False
    assert decision.context == (
        "memory: the user runs pytest -q\n\nmemory: prefers short answers"
    ), "blank contributions are dropped and the rest keep registration order"
    failures = [item.message for item in runtime.diagnostics]
    assert any("handler for `session_before_compact` raised" in message for message in failures), (
        "a raising handler is a runtime diagnostic and never a lost decision"
    )


async def test_the_compact_gate_stops_at_the_first_cancel(tmp_path):
    """A veto wins and stops the fan-out, exactly as the boolean gate did."""
    log = tmp_path / "after.log"
    runtime = _runtime(
        tmp_path,
        f"""
from pathlib import Path

from run_agent_coding.extensions.api import SessionBeforeCompactResult

LOG = Path({str(log)!r})


def setup(api):
    def cancelling(event, context):
        return SessionBeforeCompactResult(cancel=True, context="memory: too late")

    def after(event, context):
        LOG.write_text("ran", encoding="utf-8")
        return SessionBeforeCompactResult(context="memory: never merged")

    api.on("session_before_compact", cancelling)
    api.on("session_before_compact", after)
""",
    )

    decision = await runtime.emit_session_before_compact("auto")

    assert decision.cancelled is True
    assert decision.context == "memory: too late"
    assert not log.is_file(), "a cancelled compaction asks no further handler"
