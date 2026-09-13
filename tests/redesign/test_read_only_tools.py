"""Pi's read-only tool set: grep, find and ls over a real project directory."""

import subprocess

import pytest

from run_agent_coding.tools import create_all_tools, create_read_only_tools


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 'Hello'\n", encoding="utf-8")
    (tmp_path / "src" / "util.py").write_text("HELLO = 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("hello = 'ignored'\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def tool(tools, name):
    return next(item for item in tools if item.name == name)


async def test_grep_respects_gitignore_case_and_glob(project):
    grep = tool(create_read_only_tools(cwd=project), "grep")
    result = await grep.execute("1", {"pattern": "hello", "ignoreCase": True})
    assert "src/app.py:2: " in result.text
    assert "notes.md:1: hello world" in result.text
    assert "build/out.py" not in result.text
    only_py = await grep.execute("2", {"pattern": "hello", "ignoreCase": "true", "glob": "*.py"})
    assert "notes.md" not in only_py.text and "src/app.py" in only_py.text
    nothing = await grep.execute("3", {"pattern": "absent"})
    assert nothing.text == "No matches found"
    limited = await grep.execute("4", {"pattern": "hello", "ignoreCase": True, "limit": 1})
    assert "1 matches limit reached" in limited.text


async def test_find_and_ls_list_project_entries(project):
    tools = create_all_tools(cwd=project)
    found = await tools["find"].execute("1", {"pattern": "*.py"})
    assert found.text.splitlines() == ["src/app.py", "src/util.py"]
    listing = await tools["ls"].execute("2", {})
    assert listing.text.splitlines() == [".git/", ".gitignore", "build/", "notes.md", "src/"]
    assert set(tools) == {"read", "write", "edit", "bash", "grep", "find", "ls"}
    assert [item.name for item in create_read_only_tools(cwd=project)] == [
        "read",
        "grep",
        "find",
        "ls",
    ]
