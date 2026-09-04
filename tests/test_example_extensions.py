"""Behavioral tests for the shipped example extensions.

Mirrors Pi's practice of testing example extensions from the main suite
(plan-mode-extension.test.ts and friends), with one difference: instead of
mocking the extension API, these load the real files through the real
`ExtensionRuntime` and drive the composed tools — which doubles as a template
for how extension authors can test their own extensions.
"""

from pathlib import Path

import pytest

from run_agent_coding import RunAgentResourcePaths
from run_agent_coding.extensions import ExtensionRuntime

pytestmark = pytest.mark.anyio

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "extensions"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _runtime_with_examples(tmp_path: Path, *names: str) -> ExtensionRuntime:
    runtime = ExtensionRuntime()
    runtime.load(
        RunAgentResourcePaths(
            root=tmp_path / "home-tau",
            cwd=tmp_path / "project",
            agents_root=tmp_path / "home-agents",
        ),
        extra_paths=tuple(EXAMPLES_DIR / name for name in names),
        include_resource_dirs=False,
    )
    assert not [diag for diag in runtime.diagnostics if diag.severity == "error"]
    return runtime


# -- hello_tool.py -------------------------------------------------------------


def test_shipped_examples_load(tmp_path: Path) -> None:
    runtime = _runtime_with_examples(
        tmp_path,
        "hello_tool.py",
        "prompt_section.py",
        "sidebar_status.py",
    )

    assert runtime.extension_names == (
        "hello_tool",
        "prompt_section",
        "sidebar_status",
    )
    assert [tool.name for tool in runtime.extension_tools] == ["hello"]
    assert runtime.prompt_sections[0].title == "Review procedure"
    assert "```bash" in runtime.prompt_sections[0].body


async def test_hello_tool_greets(tmp_path: Path) -> None:
    runtime = _runtime_with_examples(tmp_path, "hello_tool.py")
    hello = runtime.compose_tools([])[0]

    result = await hello.execute("test-call", {"who": "Run Agent"})

    assert result.text == "Hello, Run Agent!"


async def test_hello_tool_defaults_to_world(tmp_path: Path) -> None:
    runtime = _runtime_with_examples(tmp_path, "hello_tool.py")
    hello = runtime.compose_tools([])[0]

    result = await hello.execute("test-call", {})

    assert result.text == "Hello, world!"
