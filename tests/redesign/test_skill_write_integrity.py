from pathlib import Path

import pytest

from run_agent_coding.host.learning import LearnerOwnedAsset, review_origin
from run_agent_extensions.experience import skill_manager as manager_module
from run_agent_extensions.experience.skill_guard import parse_frontmatter
from run_agent_extensions.experience.skill_manager import SkillManager, SkillRoots

BODY = "## When to Use\nRun tests before publishing."
EXTRA = (
    "compatibility: Requires Python 3.12\n"
    "allowed-tools: [read, exec]\n"
    "custom:\n  nested:\n    enabled: true\n    targets: [windows, linux]\n"
)


@pytest.fixture
def manager(tmp_path):
    manager = SkillManager(SkillRoots(tmp_path / "user", tmp_path / "project"), guard=False)
    with review_origin():
        result = manager.create("project", "build", "Build projects.", BODY)
    original = result.path.read_text(encoding="utf-8")
    original = original.replace("name: build", "# Keep this annotation.\nname: build")
    original = original.replace("license: MIT", "license: Apache-2.0").replace(
        "description: Build projects.", "description: 'Build projects.'"
    )
    result.path.write_text(original.replace("metadata:\n", EXTRA + "metadata:\n"), encoding="utf-8")
    return manager


def test_body_edit_and_patch_preserve_custom_metadata_and_noop(manager):
    path = manager.find("project", "build") / "SKILL.md"
    previous = path.read_text(encoding="utf-8")
    metadata, _ = parse_frontmatter(previous)
    usage = manager.usage["project"].load()
    ledger = manager.ledger["project"].entries()
    result = manager.edit("project", "build", None, BODY)
    assert result.changed is False and result.ledger_id is None
    assert path.read_text(encoding="utf-8") == previous
    assert manager.usage["project"].load() == usage
    assert manager.ledger["project"].entries() == ledger
    with review_origin():
        manager.view("project", "build")
        edited = manager.edit("project", "build", None, BODY + "\nCheck the output.")
        patched = manager.patch(
            "project", "build", "SKILL.md", "Check the output.", "Verify the output."
        )
    updated = path.read_text(encoding="utf-8")
    assert updated.split("---")[1] == previous.split("---")[1]
    assert parse_frontmatter(updated)[0] == metadata
    assert edited.ledger_id and patched.ledger_id
    assert len(manager.ledger["project"].entries()) == len(ledger) + 2
    assert (
        manager.usage["project"].load()["build"]["patch_count"] == usage["build"]["patch_count"] + 2
    )


@pytest.mark.parametrize(
    "description, expected",
    [(None, "From document."), ("Explicit override.", "Explicit override.")],
)
def test_full_document_merges_top_level_fields_and_keeps_identity(manager, description, expected):
    supplied = (
        "---\nname: spoofed\ncreated_by: spoofed\ndescription: From document.\n"
        "license: BSD-3-Clause\ncustom:\n  replacement: true\n---\n\nUpdated steps.\n"
    )
    manager.edit("project", "build", description, supplied)
    text = (manager.find("project", "build") / "SKILL.md").read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    assert metadata["name"] == "build"
    assert metadata["created_by"] == "review"
    assert metadata["description"] == expected
    assert metadata["license"] == "BSD-3-Clause"
    assert metadata["custom"] == {"replacement": True}
    assert metadata["compatibility"] == "Requires Python 3.12"
    assert metadata["metadata"]["run_agent"]["created_by"] == "review"
    assert body.strip() == "Updated steps."


def test_document_metadata_cannot_grant_review_ownership(manager):
    manager.create("project", "user-owned", "User instructions.", BODY)
    with review_origin(), pytest.raises(LearnerOwnedAsset, match="not curator-managed"):
        manager.edit("project", "user-owned", None, "---\ncreated_by: review\n---\nNew steps.")


@pytest.mark.parametrize("action", ["create", "edit", "patch", "write_file"])
@pytest.mark.parametrize("failure", ["replace", "flush"])
def test_failed_atomic_write_preserves_original_without_success_event(
    manager, monkeypatch, action, failure
):
    directory = manager.find("project", "build")
    target = directory / "SKILL.md"
    if action == "write_file":
        manager.write_file("project", "build", "scripts/check.py", "print('old')\n")
        target = directory / "scripts/check.py"
    elif action == "create":
        target = manager.roots.project / "new-skill" / "SKILL.md"
    original = target.read_bytes() if target.exists() else None
    usage = manager.usage["project"].load()
    ledger = manager.ledger["project"].entries()
    original_replace = manager_module.os.replace

    def fail_replace(source, destination):
        if Path(destination) == target:
            assert Path(source).read_text(encoding="utf-8")
            raise PermissionError("interrupted replacement")
        return original_replace(source, destination)

    def fail_flush(fd):
        raise OSError("interrupted flush")

    if failure == "replace":
        monkeypatch.setattr(manager_module.os, "replace", fail_replace)
    else:
        monkeypatch.setattr(manager_module.os, "fsync", fail_flush)
    with pytest.raises(OSError, match="interrupted"):
        if action == "create":
            manager.create("project", "new-skill", "New instructions.", BODY)
        elif action == "edit":
            manager.edit("project", "build", None, BODY + "\nNew steps.")
        elif action == "patch":
            manager.patch("project", "build", "SKILL.md", "Run tests", "Run checks")
        else:
            manager.write_file("project", "build", "scripts/check.py", "print('new')\n")
    assert (target.read_bytes() if target.exists() else None) == original
    assert manager.usage["project"].load() == usage
    assert manager.ledger["project"].entries() == ledger
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_atomic_support_write_preserves_existing_file_mode(manager):
    result = manager.write_file("project", "build", "scripts/check.py", "print('old')\n")
    result.path.chmod(0o755)
    mode = result.path.stat().st_mode
    manager.write_file("project", "build", "scripts/check.py", "print('new')\n")
    assert result.path.stat().st_mode == mode
    assert result.path.read_text(encoding="utf-8") == "print('new')\n"
