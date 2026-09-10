"""RED: E04, E05 and E06 over the real experience commands.

E04 - a Skill is not published without a passing report, so a proposal stays a
      candidate until an explicit publish.
E05 - rolling back points the head at an older version while the newer one stays
      in history rather than being erased.
E06 - each run yields at most one review request, so a repeated end event cannot
      produce a second review.
"""

import re
from dataclasses import replace
from pathlib import Path

from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_experience_review_wiring import FailingProvider
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication

REPO = Path(__file__).resolve().parents[2]
EXPERIENCE = REPO / "extensions" / "experience"


def experience_options(tmp_path):
    return replace(options(tmp_path), extension_paths=(EXPERIENCE,), extensions_enabled=True)


def candidate_ids(message: str) -> list[str]:
    """Candidate ids are the first field of each listed candidate line.

    A blind 32-hex regex also matches fragments of the 64-hex version ids that
    follow, so parse the line shape instead.
    """
    ids = []
    for line in message.splitlines():
        match = re.match(r"([0-9a-f]{32})\s+skill/\S+\s", line)
        if match is not None:
            ids.append(match.group(1))
    assert ids, message
    return ids


def newest_token(message: str) -> str:
    """The most recently proposed candidate, which is listed last."""
    return candidate_ids(message)[-1]


async def test_e04_a_skill_is_not_published_without_a_passing_report(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        await app.command('/experience propose project skill deploy "run tests before commit"')

        candidates = await app.command("/experience candidates project")
        assert "skill/deploy" in candidates.message, candidates.message
        # Nothing published it: a skill waits for an explicit publish.
        listed = await app.command("/experience list project")
        assert "run tests before commit" not in listed.message, listed.message

        candidate_id = candidate_ids(candidates.message)[0]
        await app.command(f"/experience publish project {candidate_id}")
        published = await app.command("/experience list project")
        assert "run tests before commit" in published.message, published.message


async def test_e05_rollback_restores_the_previous_version_and_keeps_history(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        await app.command('/experience propose project skill deploy "version one"')
        first = newest_token((await app.command("/experience candidates project")).message)
        await app.command(f"/experience publish project {first}")
        # A published head only becomes active context on an explicit reload.
        await app.command("/reload")
        listed = await app.command("/experience list project")
        assert "version one" in listed.message, listed.message
        older = re.search(r"\[([^\]]+)\]", listed.message)
        assert older is not None, listed.message

        await app.command('/experience propose project skill deploy "version two"')
        second = newest_token((await app.command("/experience candidates project")).message)
        await app.command(f"/experience publish project {second}")
        await app.command("/reload")
        newer = await app.command("/experience list project")
        assert "version two" in newer.message, newer.message

        await app.command(f"/experience rollback project skill/deploy {older.group(1)}")
        await app.command("/reload")
        rolled_back = await app.command("/experience list project")
        assert "version one" in rolled_back.message, rolled_back.message

        # History keeps the newer version instead of erasing it: its candidate
        # record survives and its frozen content is still resolvable.
        surviving = await app.command("/experience candidates project")
        assert len(candidate_ids(surviving.message)) == 2, surviving.message
        assert "version two" in (await app.command(f"/experience diff project {second}")).message


async def test_e06_a_repeated_end_event_does_not_queue_a_second_review(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=FailingProvider()
    ) as app:
        await app.start()
        settled = (await _prompt(app))[-1]
        state = context(app).services.scope("session").state
        request = await state.get(f"review-request:{settled.run_id}")
        assert request is not None

        # Deliver the same durable completion again; the key is run plus policy
        # version, so a repeat must not add anything.
        await app.session.extension_runtime.emit_event(settled)
        again = await state.get(f"review-request:{settled.run_id}")
        assert again is not None and again.version == request.version
        assert await state.get(f"review-consumed:{settled.run_id}") is None


async def _prompt(app):
    return [event async for event in app.prompt("please fail")]
