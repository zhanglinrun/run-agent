"""Curator wiring: the cadence gate, the hooks, the command surface and one E2E run."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_curator_state import write_skill

from run_agent_coding.application import CodingApplication
from run_agent_coding.events import AgentSettledEvent
from run_agent_coding.extensions.api import NullUiBridge
from run_agent_core.messages import ToolCall
from run_agent_extensions import curator as curator_package
from run_agent_extensions.curator.state import (
    DEFERRED_FIRST_RUN_SUMMARY,
    CuratorStateStore,
    due_for_run,
    to_iso,
    utc_now,
)

CURATOR_PACKAGE = Path(curator_package.__file__).parent


class ConfirmUi(NullUiBridge):
    """A UI that answers every confirmation the same way.

    ``NullUiBridge.confirm`` returns False, which is the non-interactive host behaviour,
    so a subclass with ``answer=True`` stands in for a user saying yes.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        self.calls.append((title, message))
        return self.answer


def curator_options(tmp_path, **overrides):
    base = replace(options(tmp_path), extension_paths=(CURATOR_PACKAGE,), extensions_enabled=True)
    return replace(base, **overrides)


async def open_app(tmp_path, *, ui=None, provider=None, **overrides):
    app = await CodingApplication.open(
        curator_options(tmp_path, **overrides), provider=provider or ReplyProvider()
    )
    await app.start(ui=ui or NullUiBridge())
    return app


def state_store(tmp_path) -> CuratorStateStore:
    return CuratorStateStore(options(tmp_path).paths.extension_state_dir)


def user_skills(tmp_path) -> Path:
    return options(tmp_path).paths.home / "skills"


def seed_state(tmp_path, **fields) -> None:
    store = state_store(tmp_path)
    state = store.load()
    for key, value in fields.items():
        setattr(state, key, value)
    assert store.save(state)


def report_ids(tmp_path) -> list[str]:
    return list(state_store(tmp_path).report_ids())


async def test_first_session_start_defers_the_first_run(tmp_path):
    app = await open_app(tmp_path)
    try:
        state = state_store(tmp_path).load()
        assert state.last_run_at is not None
        assert state.last_run_summary == DEFERRED_FIRST_RUN_SUMMARY
        assert state.run_count == 0
        assert state.last_report_path is None
        assert report_ids(tmp_path) == []
        assert not list(state_store(tmp_path).backups_dir.glob("*"))
    finally:
        await app.aclose()


async def test_the_interval_gate_blocks_an_early_second_run(tmp_path):
    seed_state(tmp_path, last_run_at=to_iso(utc_now()))
    app = await open_app(tmp_path)
    try:
        assert state_store(tmp_path).load().run_count == 0
        assert report_ids(tmp_path) == []
    finally:
        await app.aclose()


async def test_a_paused_curator_does_not_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_LLM_REVIEW_ENABLED", "false")
    seed_state(tmp_path, last_run_at=to_iso(utc_now() - timedelta(days=400)), paused=True)
    write_skill(user_skills(tmp_path), "old-one", age_days=400)
    app = await open_app(tmp_path)
    try:
        assert state_store(tmp_path).load().run_count == 0
        assert report_ids(tmp_path) == []
        assert (user_skills(tmp_path) / "old-one").is_dir()
    finally:
        await app.aclose()


async def test_a_due_run_snapshots_transitions_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_LLM_REVIEW_ENABLED", "false")
    seed_state(tmp_path, last_run_at=to_iso(utc_now() - timedelta(days=400)))
    write_skill(user_skills(tmp_path), "old-one", age_days=400)
    write_skill(user_skills(tmp_path), "fresh", age_days=1)
    app = await open_app(tmp_path)
    try:
        store = state_store(tmp_path)
        state = store.load()
        assert state.run_count == 1
        assert state.last_report_path is not None
        assert "1 archived" in (state.last_run_summary or "")
        assert due_for_run(state, now=utc_now(), interval_hours=168.0) is False
        assert (user_skills(tmp_path) / ".archive" / "old-one" / "SKILL.md").is_file()
        assert (user_skills(tmp_path) / "fresh").is_dir()

        run_id = Path(state.last_report_path or "").name
        payload = store.read_run(run_id)
        assert payload is not None
        assert payload["auto_transitions"]["archived"] == 1
        assert payload["snapshot"], "a mutating pass takes a pre-run snapshot"
        assert payload["snapshot"][0]["reason"] == "pre-curator-run"
        assert payload["llm_summary"] == "skipped (review disabled)"
        report = store.read_report(run_id)
        assert report is not None and "library hygiene only" in report
    finally:
        await app.aclose()


async def test_the_curator_can_be_disabled_by_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_ENABLED", "false")
    app = await open_app(tmp_path)
    try:
        assert not state_store(tmp_path).path.exists()
        assert report_ids(tmp_path) == []
    finally:
        await app.aclose()


async def test_a_hook_failure_is_swallowed_and_recorded(tmp_path):
    # A ``state.json`` that is a directory makes every state write fail, which is the
    # shape of a transient disk problem: the hooks must neither raise into the session
    # nor lose it.
    state_store(tmp_path).path.mkdir(parents=True)

    app = await open_app(tmp_path)
    try:
        await app.session.extension_runtime.emit_event(
            AgentSettledEvent(
                run_id="run-123",
                session_id=app.session.session_id,
                branch_id="branch-1",
                status="succeeded",
                head_id=None,
                watermark=1,
            )
        )
        message = (await app.command("/curator status")).message or ""
        assert "diagnostics:" in message
        assert "agent_settled:" in message
    finally:
        await app.aclose()


async def test_agent_settled_records_the_run_without_calling_inference(tmp_path):
    app = await open_app(tmp_path)
    try:
        await app.session.extension_runtime.emit_event(
            AgentSettledEvent(
                run_id="run-123",
                session_id=app.session.session_id,
                branch_id="branch-1",
                status="succeeded",
                head_id=None,
                watermark=1,
            )
        )
        state = state_store(tmp_path).load()
        assert state.last_review_run_id == "run-123"
        assert state.last_review_run_at is not None
        assert report_ids(tmp_path) == []
    finally:
        await app.aclose()


async def test_input_and_tool_call_hooks_record_consultations(tmp_path):
    app = await open_app(tmp_path)
    try:
        write_skill(user_skills(tmp_path), "deploy")
        runtime = app.session.extension_runtime
        await runtime.run_input_hooks("/skill:deploy please run it")
        await runtime.run_input_hooks("just a normal prompt")
        await runtime.before_tool_call(
            ToolCall(
                id="call-1",
                name="skill_manage",
                arguments={"action": "view", "name": "deploy", "scope": "user"},
            )
        )
        await runtime.before_tool_call(
            ToolCall(id="call-2", name="skill_manage", arguments={"action": "propose"})
        )
        await runtime.before_tool_call(
            ToolCall(id="call-3", name="read", arguments={"path": "SKILL.md"})
        )
        records = state_store(tmp_path).usage()
        assert [(record.skill, record.source) for record in records] == [
            ("deploy", "slash_command"),
            ("deploy", "skill_tool"),
        ]
    finally:
        await app.aclose()


async def test_session_shutdown_persists_the_state(tmp_path):
    app = await open_app(tmp_path)
    try:
        await app.session.extension_runtime.emit_session_shutdown("quit")
        assert state_store(tmp_path).load().last_run_at is not None
    finally:
        await app.aclose()


async def test_command_surface_usage_and_refusals(tmp_path):
    app = await open_app(tmp_path)
    try:
        usage = (await app.command("/curator")).message
        assert "/curator status" in usage and "Refused" not in usage
        for argument in ("nonsense", "status extra"):
            assert "/curator status" in (await app.command(f"/curator {argument}")).message
        unclosed = (await app.command('/curator journey show "unclosed')).message
        assert unclosed.startswith("Refused:")

        status = (await app.command("/curator status")).message
        assert "enabled: yes" in status
        assert "skills: 0" in status
        assert "would change now: 0 stale, 0 archived" in status
        assert "snapshots: 0" in status

        for command in ("/curator run", "/curator pause", "/curator restore x", "/curator review"):
            message = (await app.command(command)).message
            assert message.startswith("Refused:"), (command, message)
            assert "not confirmed" in message
        assert state_store(tmp_path).load().paused is False

        assert "Refused" in (await app.command("/curator report nope")).message
        assert "/curator status" in (await app.command("/curator learn")).message
        assert "/curator status" in (await app.command("/curator journey")).message
        assert "/curator status" in (await app.command("/curator journey list extra")).message
    finally:
        await app.aclose()


async def test_run_dry_run_is_read_only_and_writes_a_report(tmp_path):
    app = await open_app(tmp_path)
    try:
        write_skill(user_skills(tmp_path), "old-one", age_days=400)
        body = (user_skills(tmp_path) / "old-one" / "SKILL.md").read_text(encoding="utf-8")

        message = (await app.command("/curator run --dry-run")).message
        assert "(dry run)" in message
        assert (user_skills(tmp_path) / "old-one" / "SKILL.md").read_text(encoding="utf-8") == body
        assert not (user_skills(tmp_path) / ".archive").exists()

        state = state_store(tmp_path).load()
        assert state.run_count == 0  # a preview does not move the cadence clock
        payload = state_store(tmp_path).read_run(Path(state.last_report_path or "").name)
        assert payload is not None and payload["dry_run"] is True
        assert payload["auto_transitions"]["archived"] == 1
        assert payload["auto_transitions"]["planned"] == [
            {"name": "user/old-one", "from": "active", "to": "archived"}
        ]
        assert "Dry run" in (await app.command("/curator report")).message
    finally:
        await app.aclose()


async def test_confirmed_commands_change_state_and_can_be_undone(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_LLM_REVIEW_ENABLED", "false")
    ui = ConfirmUi()
    app = await open_app(tmp_path, ui=ui)
    try:
        write_skill(user_skills(tmp_path), "deploy")

        assert (await app.command("/curator pause")).message.endswith("paused.")
        assert state_store(tmp_path).load().paused is True
        assert (await app.command("/curator resume")).message.endswith("running on schedule.")
        assert state_store(tmp_path).load().paused is False

        assert "Curator pass" in (await app.command("/curator run")).message
        assert state_store(tmp_path).load().run_count == 1

        assert (
            "archived user/deploy"
            in (await app.command("/curator journey delete user/deploy")).message
        )
        assert (user_skills(tmp_path) / ".archive" / "deploy" / "SKILL.md").is_file()
        assert ui.calls[-1][0] == "Archive user/deploy?"

        assert "restored user/deploy" in (await app.command("/curator restore deploy")).message
        assert (user_skills(tmp_path) / "deploy" / "SKILL.md").is_file()
    finally:
        await app.aclose()


async def test_journey_commands_and_the_memory_refusal(tmp_path):
    app = await open_app(tmp_path)
    try:
        write_skill(user_skills(tmp_path), "deploy")
        (options(tmp_path).paths.home / "MEMORY.md").write_text(
            "remember the deploy", encoding="utf-8"
        )

        listing = (await app.command("/curator journey list")).message
        assert "user/deploy" in listing
        assert "memory:memory:0" in listing

        shown = (await app.command("/curator journey show user/deploy")).message
        assert "kind: skill" in shown and "Run the deploy" in shown
        shown_memory = (await app.command("/curator journey show memory:memory:0")).message
        assert "remember the deploy" in shown_memory
        assert "/memory" in shown_memory

        refused = (await app.command("/curator journey delete memory:memory:0")).message
        assert "owned by the memory extension" in refused
        ghost = (await app.command("/curator journey show user/ghost")).message
        assert ghost.startswith("Refused:")
    finally:
        await app.aclose()


async def test_end_to_end_status_and_dry_run_leave_every_skill_file_untouched(tmp_path):
    skill = user_skills(tmp_path) / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy safely.\ncreated_by: user\n---\n\n# Deploy\n\nRun it.\n",
        encoding="utf-8",
    )
    (skill / "references").mkdir()
    (skill / "references" / "notes.md").write_text("detail", encoding="utf-8")

    def fingerprint() -> dict[str, tuple[str, int]]:
        root = user_skills(tmp_path)
        return {
            str(path.relative_to(root)): (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = fingerprint()
    app = await open_app(tmp_path)
    try:
        events = [event async for event in app.prompt("deploy the app")]
        assert events, "one real turn ran against the fake provider"

        status = (await app.command("/curator status")).message
        assert "enabled: yes" in status
        assert "skills: 1" in status
        assert "user/deploy" in status
        assert "protected (never auto-archived): user/deploy" in status
        assert "(dry run)" in (await app.command("/curator run --dry-run")).message
        assert fingerprint() == before
    finally:
        await app.aclose()

    assert fingerprint() == before
    assert len(report_ids(tmp_path)) == 1
    payload = state_store(tmp_path).read_run(report_ids(tmp_path)[0])
    assert payload is not None and payload["dry_run"] is True
    assert payload["counts"]["before"] == 1
