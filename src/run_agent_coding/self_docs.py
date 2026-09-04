"""Locations of Run Agent's packaged self-documentation and examples."""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DATA_ROOT = _PACKAGE_ROOT / "data"


def run_agent_readme_path() -> Path:
    """Return the installed overview document for Run Agent-aware tasks."""
    return _DATA_ROOT / "docs" / "README.md"


def run_agent_docs_path() -> Path:
    """Return the installed Run Agent self-documentation directory."""
    return _DATA_ROOT / "docs"


def run_agent_examples_path() -> Path:
    """Return the installed Run Agent example directory."""
    return _DATA_ROOT / "examples"
