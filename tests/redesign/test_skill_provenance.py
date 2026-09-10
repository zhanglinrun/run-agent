"""RED: a skill a user asked for is never auto-curated (P5, T-046).

Hermes draws this line with a context variable: only skills the review fork
created are marked agent-created, and only those may ever be consolidated,
archived or pruned. Skills a user asked a foreground agent to write belong to the
user. That distinction is the only thing standing between automatic maintenance
and deleting the user's assets, so it is enforced rather than assumed.
"""

import pytest
from extensions.experience.repository import ExperienceRepository
from tests.redesign.test_coding_application import ReplyProvider, options
from tests.redesign.test_host_services import context

from run_agent_coding.application import CodingApplication
from run_agent_coding.host.learning import (
    LearnerOwnedAsset,
    is_agent_created,
    require_agent_created,
    review_origin,
    write_origin,
)


def experience_options(tmp_path):
    from dataclasses import replace
    from pathlib import Path

    experience = Path(__file__).resolve().parents[2] / "extensions" / "experience"
    return replace(options(tmp_path), extension_paths=(experience,), extensions_enabled=True)


def test_the_default_origin_is_the_foreground_learner() -> None:
    assert write_origin() == "foreground"
    assert is_agent_created() is False


def test_the_review_origin_is_scoped_to_its_block() -> None:
    with review_origin():
        assert write_origin() == "background_review"
        assert is_agent_created() is True
    assert is_agent_created() is False


def test_automatic_maintenance_refuses_a_user_written_asset() -> None:
    with pytest.raises(LearnerOwnedAsset, match="archive"):
        require_agent_created(False, "archive")
    require_agent_created(True, "archive")


async def test_a_foreground_proposal_is_not_marked_agent_created(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        await app.command('/experience propose project skill deploy "user written"')
        repository = ExperienceRepository(context(app).services, app.session.session_id)
        candidates = await repository.candidates("project")
        assert candidates and candidates[0].agent_created is False


async def test_a_review_written_proposal_is_marked_agent_created(tmp_path):
    async with await CodingApplication.open(
        experience_options(tmp_path), provider=ReplyProvider()
    ) as app:
        await app.start()
        with review_origin():
            await app.command('/experience propose project skill deploy "learned"')
        repository = ExperienceRepository(context(app).services, app.session.session_id)
        candidates = await repository.candidates("project")
        assert candidates and candidates[0].agent_created is True
