"""Normalizing and cloning Git extension sources.

Turns a ``git+https://host/owner/repo@ref`` style source into a validated name and
clone arguments, and checks the cloned tree against the same discovery rules the
user extension directory obeys.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

SUPPORTED_SCHEMES = ("https://", "http://", "ssh://", "git://", "file://", "git@")
INSTALL_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"


class ExtensionInstallError(RuntimeError):
    """Raised when an extension source cannot be installed safely."""


@dataclass(frozen=True, slots=True)
class GitExtensionSource:
    """A normalized Git repository source and optional checkout ref."""

    url: str
    ref: str | None
    name: str


def validate_install_name(name: str) -> None:
    """Refuse a name that would escape or collide when used as a directory."""
    if not re.fullmatch(INSTALL_NAME_PATTERN, name) or name in {".", ".."}:
        raise ExtensionInstallError(f"extension source has an unsupported install name: {name!r}")


def parse_git_extension_source(source: str) -> GitExtensionSource:
    """Normalize a Pi-style Git source accepted by ``run-agent install``."""
    raw = source[4:] if source.startswith("git:") else source
    if not raw:
        raise ExtensionInstallError("Git extension source is empty")

    ref: str | None = None
    last_at = raw.rfind("@")
    last_separator = max(raw.rfind("/"), raw.rfind(":"))
    if last_at > last_separator:
        raw, ref = raw[:last_at], raw[last_at + 1 :]
        if not ref:
            raise ExtensionInstallError("Git extension ref is empty")

    url = _normalize_url(raw)
    name = raw.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    validate_install_name(name)
    return GitExtensionSource(url=url, ref=ref, name=name)


def _normalize_url(raw: str) -> str:
    if raw.startswith("github.com/"):
        return f"https://{raw}"
    if raw.startswith(SUPPORTED_SCHEMES):
        return raw
    raise ExtensionInstallError(
        "extension source does not exist locally and is not a supported Git source; "
        "use a local path, git:github.com/owner/repo, or a Git URL"
    )


def clone_git_source(
    source: GitExtensionSource,
    destination: Path,
    *,
    command_runner: CommandRunner,
) -> None:
    """Clone the source, then detach at its ref when one was given."""
    _git(
        command_runner,
        ["clone", "--", source.url, str(destination)],
        f"could not clone {source.url}",
    )
    if source.ref is not None:
        _git(
            command_runner,
            ["-C", str(destination), "checkout", "--detach", source.ref],
            f"could not check out ref {source.ref!r}",
        )


def _git(command_runner: CommandRunner, arguments: list[str], failure: str) -> None:
    result = command_runner(["git", *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise ExtensionInstallError(f"{failure}: {detail}")
