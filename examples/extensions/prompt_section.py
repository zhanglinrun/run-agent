"""Append structured, always-on instructions to Run Agent's system prompt."""

from run_agent_coding.extensions import ExtensionAPI


def setup(api: ExtensionAPI) -> None:
    """Add a labeled procedure while this extension generation is active."""
    api.add_prompt_section(
        "Review procedure",
        """Read the complete diff before editing.

Run the relevant checks before reporting success:

```bash
uv run pytest
uv run ruff check .
```
""",
    )
