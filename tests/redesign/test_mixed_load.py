"""P3-8: one session interleaves foreground, background and control requests.

Plan 11.1 asks that a single session encounter foreground submissions, a
background submission and control requests in the same run, rather than binding a
task lane to a session the way the earlier benchmark did. The script below is
fixed, so re-running it replays the same sequence and checks the same invariants.
"""

import asyncio
from pathlib import Path

from tests.redesign.git_helpers import create_repository
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_gateway_runtime import eventually, released, submit

from run_agent_coding.storage.sqlite import SqliteDatabase
from run_agent_core.messages import UserMessage
from run_agent_core.session.entries import MessageEntry
from run_agent_gateway.coding import CodingAssignmentRunner
from run_agent_gateway.contracts import RouteIdentity, Submission
from run_agent_gateway.controller import SessionController
from run_agent_gateway.repository import GatewayRepository
from run_agent_gateway.runtime import GatewayCodingRuntime
from run_agent_gateway.scheduler import GatewayScheduler

# Fixed script: foreground work, an independent background task, a control
# request that must not queue behind the model, then more foreground work.
MIXED_LOAD_SCRIPT = (
    ("foreground", "f-1"),
    ("background", "b-1"),
    ("control", "/status"),
    ("foreground", "f-2"),
    ("foreground", "f-3"),
)
FOREGROUND_TEXTS = tuple(text for kind, text in MIXED_LOAD_SCRIPT if kind == "foreground")


def background_submission(workspace: Path, message: str, text: str) -> Submission:
    """A background task on the same route, which owns an independent workspace."""
    return Submission(
        RouteIdentity("local", "account", "chat", subject_id="alice"),
        "alice",
        message,
        text,
        workspace,
        lane="background",
    )


def session_user_texts(entries) -> list[str]:
    return [
        entry.message.text
        for entry in entries
        if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage)
    ]


async def test_one_session_replays_the_mixed_foreground_background_control_script(tmp_path):
    opts = options(tmp_path)
    # A background task owns an independent worktree, so its workspace must be a
    # real repository rather than a bare directory.
    project = create_repository(tmp_path / "project")
    async with await SqliteDatabase.open(opts.paths.database_path) as database:
        repository = GatewayRepository(database)
        await repository.initialize()
        owner = await repository.acquire_owner("mixed-load-host")
        host = GatewayCodingRuntime(
            repository, owner, opts, provider_factory=lambda _: ReplyProvider()
        )
        scheduler = GatewayScheduler(repository, owner, CodingAssignmentRunner(host))
        await scheduler.start()
        try:
            admitted = []
            control = None
            for kind, text in MIXED_LOAD_SCRIPT:
                if kind == "control":
                    control = await SessionController(repository).handle(
                        owner, submit(project, text, text), model="test"
                    )
                    continue
                submission = (
                    submit(project, text, text)
                    if kind == "foreground"
                    else background_submission(project, text, text)
                )
                admitted.append((kind, await repository.admit(owner, submission, model="test")))

            await eventually(
                lambda: _all_released(repository, [item.task_id for _, item in admitted])
            )

            assert isinstance(control, dict) and control, control
            assert not scheduler.errors and scheduler.failure is None
            foreground_ids = {item.session_id for kind, item in admitted if kind == "foreground"}
            background_ids = {item.session_id for kind, item in admitted if kind == "background"}
            assert len(foreground_ids) == 1, foreground_ids
            # A background task reads a fixed snapshot in its own cloned session,
            # so it must not share the foreground session's history.
            assert background_ids and background_ids.isdisjoint(foreground_ids)
            session_id = next(iter(foreground_ids))
            entries = (await repository.sessions.read_entries(session_id)).entries
            texts = session_user_texts(entries)
            foreground = [text for text in texts if text in FOREGROUND_TEXTS]
            assert foreground == list(FOREGROUND_TEXTS), foreground
            # A control request is answered by the controller, so it never
            # becomes model input the way a normal submission does.
            assert "/status" not in texts, texts
        finally:
            await scheduler.shutdown()
            await repository.release_owner(owner)


async def _all_released(repository: GatewayRepository, task_ids: list[str]) -> bool:
    results = await asyncio.gather(*(released(repository, task_id) for task_id in task_ids))
    return all(results)
