"""The journey view: node ids, list/show output and the delete ownership split."""

from __future__ import annotations

from tests.redesign.test_curator_state import make_env, write_skill

from run_agent_coding.paths import RunAgentPaths
from run_agent_extensions.curator.journey import (
    MEMORY_REFUSAL,
    delete_node,
    is_memory_id,
    journey_nodes,
    memory_entries,
    parse_memory_id,
    render_list,
    render_show,
)


def make_paths(tmp_path):
    return RunAgentPaths(home=tmp_path / "home", agents_home=tmp_path / "agents")


def test_memory_ids_count_non_empty_blocks(tmp_path):
    paths = make_paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "MEMORY.md").write_text("first block\n§\n\n§\nsecond block", encoding="utf-8")
    (paths.home / "USER.md").write_text("prefers terse output", encoding="utf-8")

    entries = memory_entries(paths, tmp_path / "project")
    assert [entry.id for entry in entries] == [
        "memory:memory:0",
        "memory:memory:1",
        "memory:profile:2",
    ]
    assert [entry.local_index for entry in entries] == [0, 1, 0]
    assert entries[0].title == "first block"
    assert entries[2].source == "profile"
    assert entries[2].path.name == "USER.md"


def test_the_project_memory_file_is_indexed_after_the_user_one(tmp_path):
    paths = make_paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "MEMORY.md").write_text("user fact", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".run").mkdir(parents=True)
    (project / ".run" / "MEMORY.md").write_text("project fact", encoding="utf-8")

    entries = memory_entries(paths, project)
    assert [entry.id for entry in entries] == ["memory:memory:0", "memory:memory:1"]
    assert [entry.path.parent.name for entry in entries] == ["home", ".run"]
    assert entries[0].path.parent == paths.home
    assert entries[1].path.parent == project / ".run"


def test_missing_memory_files_contribute_nothing(tmp_path):
    paths = make_paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    assert memory_entries(paths, tmp_path / "project") == ()


def test_a_memory_id_carries_its_source_and_index():
    assert is_memory_id("memory:memory:0") is True
    assert is_memory_id("user/deploy") is False
    assert parse_memory_id("memory:profile:3") == ("profile", 3)
    assert parse_memory_id("memory:nope:3") is None
    assert parse_memory_id("memory:memory:x") is None


def test_journey_nodes_use_scope_qualified_skill_ids(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    write_skill(env.project_root, "project-deploy")
    paths = make_paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "MEMORY.md").write_text("remember this", encoding="utf-8")

    nodes = journey_nodes(env.library.records(), memory_entries(paths, env.project))
    assert [node.id for node in nodes] == [
        "user/deploy",
        "project/project-deploy",
        "memory:memory:0",
    ]
    assert [node.kind for node in nodes] == ["skill", "skill", "memory"]


def test_render_list_shows_every_node(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    nodes = journey_nodes(env.library.records(), ())
    text = render_list(nodes)
    assert "user/deploy" in text
    assert "created_by=evolution" in text
    assert render_list(()) == "Nothing to show yet: no Skills and no memory blocks."


def test_render_show_for_a_skill_and_a_memory(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", body="Run the deploy.")
    paths = make_paths(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "MEMORY.md").write_text("remember the deploy", encoding="utf-8")
    records = env.library.records()
    entries = memory_entries(paths, env.project)

    skill = render_show("user/deploy", records=records, entries=entries, library=env.library)
    assert "id: user/deploy" in skill
    assert "kind: skill" in skill
    assert "Run the deploy." in skill
    assert "protected: no" in skill

    memory = render_show("memory:memory:0", records=records, entries=entries, library=env.library)
    assert "kind: memory" in memory
    assert "remember the deploy" in memory
    assert MEMORY_REFUSAL in memory

    refused_skill = render_show("user/ghost", records=records, entries=entries, library=env.library)
    assert refused_skill.startswith("Refused:")
    refused_memory = render_show(
        "memory:memory:9", records=records, entries=entries, library=env.library
    )
    assert refused_memory.startswith("Refused:")


def test_delete_of_a_skill_archives_it(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy", body="Keep this text.")
    mutation = delete_node("user/deploy", records=env.library.records(), library=env.library)
    assert mutation.ok is True
    archived = env.user_root / ".archive" / "deploy" / "SKILL.md"
    assert archived.is_file()
    assert "Keep this text." in archived.read_text(encoding="utf-8")
    assert env.library.records() == ()
    assert mutation.ledger_id is not None


def test_delete_of_a_memory_is_refused(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    records = env.library.records()
    mutation = delete_node("memory:memory:0", records=records, library=env.library)
    assert mutation.ok is False
    assert mutation.message.startswith("Refused:")
    assert "/memory" in mutation.message


def test_delete_of_a_pinned_skill_is_refused(tmp_path):
    env = make_env(tmp_path)
    write_skill(env.user_root, "deploy")
    env.manager.usage["user"].set_pinned("deploy", True)
    mutation = delete_node("user/deploy", records=env.library.records(), library=env.library)
    assert mutation.ok is False
    assert "pinned" in mutation.message
    assert (env.user_root / "deploy").is_dir()


def test_delete_of_an_unknown_id_is_refused(tmp_path):
    env = make_env(tmp_path)
    mutation = delete_node("project/ghost", records=env.library.records(), library=env.library)
    assert mutation.ok is False
    assert "no Skill node" in mutation.message
