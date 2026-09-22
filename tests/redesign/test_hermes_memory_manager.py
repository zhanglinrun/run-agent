"""MemoryManager fan-out, recall indicator, commit queue and drain semantics."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence

import pytest

from run_agent_core.messages import AgentMessage, UserMessage
from run_agent_extensions.hermes_memory import (
    FlushResult,
    MemoryManager,
    MemoryProvider,
    RecallStatus,
)
from run_agent_extensions.hermes_memory import (
    build_memory_context_block as fence,
)


class FakeProvider(MemoryProvider):
    """Recording provider whose every hook can be made to fail on demand."""

    def __init__(
        self,
        name: str,
        *,
        schemas: Sequence[Mapping[str, object]] = (),
        prefetch_text: str = "",
        status: RecallStatus | None = None,
        delay: float = 0.0,
    ) -> None:
        self._name = name
        self._schemas = [dict(schema) for schema in schemas]
        self._prefetch_text = prefetch_text
        self._status = status
        self._delay = delay
        self.calls: list[str] = []
        self.fail: set[str] = set()

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: object) -> None:
        self.calls.append(f"initialize:{session_id}:{kwargs.get('home', '')}")
        if "initialize" in self.fail:
            raise RuntimeError("initialize exploded")

    def get_tool_schemas(self) -> list[Mapping[str, object]]:
        if "schemas" in self.fail:
            raise RuntimeError("get_tool_schemas exploded")
        return list(self._schemas)

    def system_prompt_block(self) -> str:
        if "prompt" in self.fail:
            raise RuntimeError("system_prompt_block exploded")
        return f"{self._name} block"

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.calls.append(f"prefetch:{query}:{session_id}")
        if "prefetch" in self.fail:
            raise RuntimeError("prefetch exploded")
        return self._prefetch_text

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self.calls.append(f"queue:{query}:{session_id}")
        if "queue" in self.fail:
            raise RuntimeError("queue_prefetch exploded")

    def recall_status(self) -> RecallStatus | None:
        return self._status

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Sequence[AgentMessage] | None = None,
    ) -> None:
        if self._delay:
            time.sleep(self._delay)
        self.calls.append(f"sync:{user_content}:{len(messages or ())}")
        if "sync" in self.fail:
            raise RuntimeError("sync_turn exploded")

    def handle_tool_call(self, tool_name: str, args: Mapping[str, object], **kwargs: object) -> str:
        if "tool" in self.fail:
            raise RuntimeError("handle_tool_call exploded")
        return json.dumps({"success": True, "tool": tool_name, "args": dict(args)})

    def on_turn_start(self, turn_number: int, message: str, **kwargs: object) -> None:
        self.calls.append(f"turn:{turn_number}:{message}")

    def on_session_end(self, messages: Sequence[AgentMessage]) -> None:
        self.calls.append("end")
        if "end" in self.fail:
            raise RuntimeError("on_session_end exploded")

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: object,
    ) -> None:
        self.calls.append(f"switch:{new_session_id}:{reset}:{rewound}:{parent_session_id}")
        if "switch" in self.fail:
            raise RuntimeError("on_session_switch exploded")

    def on_pre_compress(self, messages: Sequence[AgentMessage]) -> str:
        self.calls.append("precompress")
        return f"{self._name} pre-compress contribution"

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.calls.append(f"mirror:{action}:{target}:{content}:{dict(metadata or {})}")

    def shutdown(self) -> None:
        self.calls.append("shutdown")


def test_builtin_is_always_accepted_and_only_one_external_provider_runs() -> None:
    manager = MemoryManager()
    manager.add_provider(FakeProvider("builtin"))
    first = FakeProvider("first")
    second = FakeProvider("second")
    manager.add_provider(first)
    manager.add_provider(second)

    assert [provider.name for provider in manager.providers] == ["builtin", "first"]
    assert manager.get_provider("second") is None
    assert manager.get_provider("builtin") is not None


def test_reserved_tool_names_are_rejected_and_routing_is_deduplicated() -> None:
    manager = MemoryManager()
    provider = FakeProvider(
        "external",
        schemas=[
            {"name": "read", "description": "shadows the core tool"},
            {"name": "custom_memory", "description": "d", "parameters": {}},
            {"description": "no name at all"},
            {"type": "function", "function": {"name": "wrapped_memory", "parameters": {}}},
        ],
    )
    manager.add_provider(provider)

    assert manager.get_all_tool_names() == {"custom_memory", "wrapped_memory"}
    assert manager.has_tool("read") is False
    assert [schema["name"] for schema in manager.get_all_tool_schemas()] == [
        "custom_memory",
        "wrapped_memory",
    ]


def test_get_all_tool_schemas_isolates_a_failing_provider() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin", schemas=[{"name": "a", "parameters": {}}])
    broken.fail.add("schemas")
    manager.add_provider(broken)
    manager.add_provider(FakeProvider("external", schemas=[{"name": "b", "parameters": {}}]))

    assert [schema["name"] for schema in manager.get_all_tool_schemas()] == ["b"]


def test_prefetch_failure_is_isolated_from_the_other_provider_and_the_caller() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.add("prefetch")
    manager.add_provider(broken)
    healthy = FakeProvider("external", prefetch_text="recalled fact")
    manager.add_provider(healthy)

    assert manager.prefetch_all("what do I prefer?") == "recalled fact"
    assert healthy.calls == ["prefetch:what do I prefer?:"]


def test_prefetch_all_survives_every_provider_failing() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.add("prefetch")
    manager.add_provider(broken)
    external = FakeProvider("external")
    external.fail.add("prefetch")
    manager.add_provider(external)

    assert manager.prefetch_all("query") == ""


def test_sync_and_queue_failures_are_isolated() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.update({"sync", "queue"})
    manager.add_provider(broken)
    healthy = FakeProvider("external")
    manager.add_provider(healthy)

    manager.sync_all("user turn", "assistant turn", messages=(UserMessage(content="user turn"),))
    manager.queue_prefetch_all("user turn")
    assert manager.flush_pending(5).status == "drained"
    assert healthy.calls == ["sync:user turn:1", "queue:user turn:"]


def test_system_prompt_isolates_failures_and_labels_providers() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.add("prompt")
    manager.add_provider(broken)
    manager.add_provider(FakeProvider("external"))

    assert manager.build_system_prompt() == "external block"


def test_initialize_all_isolates_failures_and_passes_home() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.add("initialize")
    manager.add_provider(broken)
    healthy = FakeProvider("external")
    manager.add_provider(healthy)

    manager.initialize_all("session-1", home="/home/.run")
    assert healthy.calls == ["initialize:session-1:/home/.run"]


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "🧠 Honcho — recalled relevant memory"),
        (-1, "🧠 Honcho — recalled relevant memory"),
        (1, "🧠 Honcho — recalled 1 memory"),
        (3, "🧠 Honcho — recalled 3 memories"),
    ],
)
def test_describe_recall_texts(count: int, expected: str) -> None:
    manager = MemoryManager()
    assert manager.describe_recall(RecallStatus("Honcho", count)) == expected


def test_describe_recall_uses_provider_glyphs_and_reports_nothing_when_empty() -> None:
    manager = MemoryManager()
    assert manager.describe_recall() == ""
    manager.add_provider(FakeProvider("builtin"))
    assert manager.describe_recall() == ""
    assert manager.describe_recall(RecallStatus("Hindsight", 2, "👁️")) == (
        "👁️ Hindsight — recalled 2 memories"
    )


def test_recall_status_reflects_only_the_last_prefetch() -> None:
    manager = MemoryManager()
    first = FakeProvider("builtin", prefetch_text="a", status=RecallStatus("builtin", 4))
    manager.add_provider(first)
    external = FakeProvider("external", prefetch_text="b", status=RecallStatus("external", 2))
    manager.add_provider(external)

    assert manager.prefetch_all("q") == "a\n\nb"
    assert manager.recall_status() == RecallStatus("external", 2)
    assert manager.recall_statuses() == (RecallStatus("builtin", 4), RecallStatus("external", 2))
    assert manager.describe_recall() == (
        "🧠 builtin — recalled 4 memories  🧠 external — recalled 2 memories"
    )

    external._status = None
    assert manager.prefetch_all("q") == "a\n\nb"
    assert manager.recall_status() == RecallStatus("builtin", 4)
    first._status = None
    assert manager.prefetch_all("q") == "a\n\nb"
    assert manager.recall_status() is None


def test_prefetch_context_feeds_the_request_fence() -> None:
    manager = MemoryManager()
    manager.add_provider(FakeProvider("builtin"))
    manager.add_provider(FakeProvider("external", prefetch_text="the user prefers pytest"))

    block = fence(manager.prefetch_all("what do I prefer?"))
    assert "<memory-context>" in block
    assert "the user prefers pytest" in block


def test_background_queue_is_fifo_and_the_boundary_is_end_then_switch() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")
    manager.add_provider(provider)

    manager.sync_all("u1", "a1")
    manager.queue_prefetch_all("u1")
    manager.commit_session_boundary_async(
        (UserMessage(content="u1"),), new_session_id="s2", parent_session_id="s1"
    )

    assert manager.flush_pending(5) == FlushResult()
    assert provider.calls == [
        "sync:u1:0",
        "queue:u1:",
        "end",
        "switch:s2:True:False:s1",
    ]


def test_boundary_still_fires_switch_when_end_raises_on_a_provider() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")
    provider.fail.add("end")
    manager.add_provider(provider)

    manager.commit_session_boundary_async((UserMessage(content="u"),), new_session_id="s2")
    assert manager.flush_pending(5).status == "drained"
    assert provider.calls == ["end", "switch:s2:True:False:"]


def test_boundary_still_fires_switch_when_the_end_fan_out_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")
    manager.add_provider(provider)

    def explode(messages: Sequence[AgentMessage]) -> None:
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(manager, "on_session_end", explode)
    manager.commit_session_boundary_async((UserMessage(content="u"),), new_session_id="s2")

    assert manager.flush_pending(5).status == "drained"
    assert provider.calls == ["switch:s2:True:False:"]


def test_session_switch_ignore_empty_id_and_forwards_rewound_only_when_set() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")
    manager.add_provider(provider)

    manager.on_session_switch("")
    assert provider.calls == []

    manager.on_session_switch("s2", parent_session_id="s1", reset=False)
    manager.on_session_switch("s2", rewound=True)
    assert provider.calls == [
        "switch:s2:False:False:s1",
        "switch:s2:False:True:",
    ]


def test_session_switch_failure_is_isolated() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    broken.fail.add("switch")
    manager.add_provider(broken)

    manager.on_session_switch("s2")
    assert broken.calls == ["switch:s2:False:False:"]


def test_flush_pending_reports_abandoned_writes_instead_of_dropping_them() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin", delay=0.4)
    manager.add_provider(provider)

    for index in range(3):
        manager.sync_all(f"u{index}", "a")

    result = manager.flush_pending(timeout=0.05)
    assert result.status == "timed_out"
    assert result.abandoned_writes == 2
    assert result.abandoned_prefetches == 0
    assert result.active_tasks == 1

    # Repeated calls with no new submission in between return the recorded outcome,
    # and a shutdown still gives the in-flight write its bounded window.
    assert manager.flush_pending(timeout=0.05) == result
    assert manager.flush_pending(timeout=5) == result

    # A real shutdown gives the in-flight write its bounded window and reports a
    # clean drain once it lands.
    assert manager.shutdown_all(timeout=5) == FlushResult()
    assert provider.calls == ["sync:u0:0", "shutdown"]


def test_shutdown_drain_reports_abandoned_prefetches_separately() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")

    def slow_queue(query: str, *, session_id: str = "") -> None:
        time.sleep(0.4)
        provider.calls.append(f"queue:{query}")

    provider.queue_prefetch = slow_queue  # type: ignore[method-assign]
    manager.add_provider(provider)

    manager.queue_prefetch_all("first")
    manager.queue_prefetch_all("second")

    result = manager.shutdown_all(timeout=0.05)
    assert result.status == "timed_out"
    assert result.abandoned_writes == 0
    assert result.abandoned_prefetches == 1
    assert result.active_tasks == 1


def test_flush_pending_is_a_noop_without_background_work() -> None:
    manager = MemoryManager()
    manager.add_provider(FakeProvider("builtin"))
    assert manager.flush_pending(timeout=1) == FlushResult()
    assert manager.flush_pending(timeout=1) == FlushResult()


def test_submissions_after_shutdown_are_rejected_without_raising() -> None:
    manager = MemoryManager()
    provider = FakeProvider("builtin")
    manager.add_provider(provider)

    assert manager.shutdown_all(timeout=1).status == "drained"
    manager.sync_all("late", "turn")
    manager.queue_prefetch_all("late")
    assert manager.flush_pending(timeout=1).status == "drained"
    assert provider.calls == ["shutdown"]
    # shutdown_all is idempotent.
    assert manager.shutdown_all(timeout=1).status == "drained"


def test_tool_routing_returns_json_and_isolates_failures() -> None:
    manager = MemoryManager()
    provider = FakeProvider("external", schemas=[{"name": "custom_memory", "parameters": {}}])
    manager.add_provider(provider)

    payload = json.loads(manager.handle_tool_call("custom_memory", {"query": "x"}))
    assert payload["success"] is True

    provider.fail.add("tool")
    failed = json.loads(manager.handle_tool_call("custom_memory", {}))
    assert failed["success"] is False
    assert "handle_tool_call exploded" in failed["error"]

    unhandled = json.loads(manager.handle_tool_call("nope", {}))
    assert unhandled["success"] is False
    assert "No memory provider handles tool 'nope'" in unhandled["error"]


def test_on_memory_write_mirrors_to_external_providers_only() -> None:
    manager = MemoryManager()
    builtin = FakeProvider("builtin")
    manager.add_provider(builtin)
    external = FakeProvider("external")
    manager.add_provider(external)

    manager.on_memory_write("add", "user", "prefers pytest", {"write_origin": "tool"})

    assert builtin.calls == []
    assert external.calls == ["mirror:add:user:prefers pytest:{'write_origin': 'tool'}"]


def test_on_turn_start_pre_compress_and_delegation_fan_out() -> None:
    manager = MemoryManager()
    broken = FakeProvider("builtin")
    manager.add_provider(broken)
    healthy = FakeProvider("external")
    manager.add_provider(healthy)

    manager.on_turn_start(3, "hello")
    assert broken.calls == ["turn:3:hello"]
    assert manager.on_pre_compress((UserMessage(content="x"),)) == (
        "builtin pre-compress contribution\n\nexternal pre-compress contribution"
    )
    manager.on_delegation("task", "result", child_session_id="child-1")
