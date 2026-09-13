"""Hermes-inspired tool contracts exercised against isolated real stores."""

import pytest

from run_agent_coding.host.learning import review_origin
from run_agent_extensions.experience import memory as memory_module
from run_agent_extensions.experience.config import ExperienceConfig
from run_agent_extensions.experience.memory import ENTRY_DELIMITER, MemoryFile, MemoryStore
from run_agent_extensions.experience.review_agent import ToolCallRecord, summarize_actions
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots
from run_agent_extensions.experience.stores import ExperienceStores
from run_agent_extensions.experience.tools import run_memory_tool, run_skill_tool

BODY = "# Build\n\nRun pytest.\nRun pytest.\n"


@pytest.fixture
def stores(tmp_path):
    return ExperienceStores(
        memory={
            "user": MemoryStore(tmp_path / "user", {"memory": 40}),
            "project": MemoryStore(tmp_path / "project", {"memory": 40}),
        },
        skills=SkillManager(SkillRoots(tmp_path / "user-skills", tmp_path / "project-skills")),
        config=ExperienceConfig(),
    )


async def test_memory_batch_alias_consolidates_against_final_budget_and_reloads(stores):
    memory = stores.memory["project"].file("memory")
    assert memory.add("a" * 20).accepted
    assert memory.add("b" * 15).accepted
    result = await run_memory_tool(
        stores,
        {
            "action": "batch",
            "operations": [
                {"action": "add", "new_text": "c" * 15},
                {"action": "replace", "old_text": "a" * 20, "new_text": "b" * 15},
            ],
        },
    )
    assert result.details["accepted"] and result.details["done"]
    assert memory.entries == ("b" * 15, "c" * 15)
    memory.load()
    assert memory.entries == ("b" * 15, "c" * 15)
    assert memory.replace("b" * 15, "c" * 15).accepted
    assert memory.entries == ("c" * 15,)
    assert memory.remove("c" * 15).accepted  # no self-created drift after consolidation


@pytest.mark.parametrize("action", ["add", "replace", "batch"])
async def test_memory_delimiters_cannot_corrupt_the_next_reload(stores, action):
    memory = stores.memory["project"].file("memory")
    assert memory.add("original").accepted
    content = "one" + ENTRY_DELIMITER + "two"
    arguments = {"action": action, "content": content, "old_text": "original"}
    if action == "batch":
        arguments = {"action": "batch", "operations": [{"action": "add", "content": content}]}
    result = await run_memory_tool(stores, arguments)
    assert not result.details["accepted"]
    assert "round-trip" in result.text
    assert memory.entries == ("original",)
    assert memory.path.read_text(encoding="utf-8") == "original"


def test_memory_multiline_windows_text_roundtrips(tmp_path):
    memory = MemoryFile(tmp_path / "MEMORY.md", 100)
    assert memory.add("Build\r\nwith tests").accepted
    assert memory.entries == ("Build\nwith tests",)
    assert memory.replace("Build\r\nwith tests", "Check\r\ntests").accepted
    assert memory.apply_batch(
        [{"action": "replace", "old_text": "Check\r\ntests", "new_text": "Run\r\nchecks"}]
    ).accepted
    memory.load()
    assert memory.entries == ("Run\nchecks",)
    assert memory.remove("Run\r\nchecks").accepted


async def test_memory_failed_disk_commit_keeps_live_state_and_allows_retry(stores, monkeypatch):
    memory = stores.memory["project"].file("memory")
    assert memory.add("original").accepted
    original_writer = memory_module._atomic_write

    def fail_write(path, content):
        raise PermissionError("file temporarily locked")

    monkeypatch.setattr(memory_module, "_atomic_write", fail_write)
    result = await run_memory_tool(
        stores, {"action": "replace", "old_text": "original", "content": "new"}
    )
    assert not result.details["accepted"] and not result.details["done"]
    assert result.details["current_entries"] == ["original"]
    assert memory.entries == ("original",)
    assert memory.path.read_text(encoding="utf-8") == "original"
    monkeypatch.setattr(memory_module, "_atomic_write", original_writer)
    assert memory.replace("original", "new").accepted


def test_memory_add_does_not_rewrite_noncanonical_external_entries(tmp_path):
    path = tmp_path / "MEMORY.md"
    raw = ENTRY_DELIMITER.join(["fact", "fact"])
    path.write_text(raw, encoding="utf-8")
    memory = MemoryFile(path, 100)
    result = memory.add("new")
    assert not result.accepted and result.backup
    assert path.read_text(encoding="utf-8") == raw


async def test_skill_tool_requires_explicit_deletion_and_exposes_replace_all(stores):
    manager = stores.skills
    manager.create("project", "build", "Build projects.", BODY)
    original = manager.view("project", "build")
    ledger_count = len(manager.ledger["project"].entries())
    missing = await run_skill_tool(
        stores, {"action": "patch", "name": "build", "old_text": "Run pytest."}
    )
    assert not missing.details["accepted"] and "requires new_text" in missing.text
    ambiguous = await run_skill_tool(
        stores,
        {"action": "patch", "name": "build", "old_text": "Run pytest.", "new_text": "Run tests."},
    )
    assert not ambiguous.details["accepted"] and "replace_all=true" in ambiguous.text
    assert manager.view("project", "build") == original
    assert len(manager.ledger["project"].entries()) == ledger_count
    patched = await run_skill_tool(
        stores,
        {
            "action": "patch",
            "name": "build",
            "old_text": "Run pytest.",
            "new_text": "Run tests.",
            "replace_all": True,
        },
    )
    assert patched.details["accepted"] and patched.details["changed"]
    assert manager.view("project", "build").count("Run tests.") == 2
    deleted = await run_skill_tool(
        stores,
        {
            "action": "patch",
            "name": "build",
            "old_text": "Run tests.",
            "new_text": "",
            "replace_all": True,
        },
    )
    assert deleted.details["accepted"]
    assert "Run tests." not in manager.view("project", "build")


async def test_identical_skill_writes_do_not_inflate_usage_or_ledger(stores):
    manager = stores.skills
    manager.create("project", "build", "Build projects.", BODY)
    manager.write_file("project", "build", "references/check.md", "Run tests.\n")
    previous_usage = manager.usage["project"].load()
    previous_ledger = manager.ledger["project"].entries()
    for arguments in [
        {"action": "edit", "name": "build", "body": BODY},
        {
            "action": "write_file",
            "name": "build",
            "file_path": "references/check.md",
            "content": "Run tests.\n",
        },
    ]:
        result = await run_skill_tool(stores, arguments)
        assert result.details["accepted"] and result.details["changed"] is False
        assert "ledger_id" not in result.details
        record = ToolCallRecord("skill_manage", arguments, result.text, True, result.details)
        assert summarize_actions([record]) == []
        assert summarize_actions([record], mode="verbose") == []
    assert manager.usage["project"].load() == previous_usage
    assert manager.ledger["project"].entries() == previous_ledger
    with review_origin():
        # Even an identical write must first satisfy the review's read guard.
        denied = await run_skill_tool(
            stores,
            {
                "action": "write_file",
                "name": "build",
                "file_path": "references/check.md",
                "content": "Run tests.\n",
            },
        )
        assert not denied.details["accepted"]
        assert "not loaded" in denied.text or "not curator-managed" in denied.text
