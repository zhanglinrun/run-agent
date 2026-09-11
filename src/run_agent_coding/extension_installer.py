"""Install Run Agent extensions from local paths or Git repositories."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from tempfile import mkdtemp

from run_agent_coding.extension_git_source import (
    CommandRunner,
    ExtensionInstallError,
    GitExtensionSource,
    clone_git_source,
    parse_git_extension_source,
    validate_install_name,
)
from run_agent_coding.extensions import discover_extensions
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.resources import RunAgentResourcePaths


def install_extension(
    source: str,
    *,
    force: bool = False,
    extensions_dir: Path | None = None,
    command_runner: CommandRunner = subprocess.run,
) -> Path:
    """Install one extension into Run Agent's user extension directory.

    Local Python files are copied. Local package directories and Git
    repositories must expose ``extension.py`` or ``[tool.run].extensions`` so
    the normal user-directory discovery path can load them on the next run.
    """
    destination_root = extensions_dir or RunAgentPaths().user_extensions_dir
    staging_root: Path | None = None
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        local_source = Path(source).expanduser()
        resolved_source: Path | None = None
        git_source: GitExtensionSource | None = None
        if local_source.exists():
            resolved_source = local_source.resolve()
            name = _local_install_name(resolved_source)
        else:
            git_source = parse_git_extension_source(source)
            name = git_source.name
        destination = destination_root / name

        # Reproduce the exact ~/.run/extensions/<name> shape during validation.
        # This catches layouts that explicit -e discovery accepts but normal
        # user-directory discovery would skip, such as nested/extension.py.
        staging_root = Path(mkdtemp(prefix=".extension-install-", dir=destination_root))
        staging = staging_root / "extensions" / name
        staging.parent.mkdir()
        if resolved_source is not None and resolved_source.is_file():
            shutil.copy2(resolved_source, staging)
        elif resolved_source is not None and resolved_source.is_dir():
            shutil.copytree(resolved_source, staging, ignore=_local_copy_ignore)
        elif git_source is not None:
            clone_git_source(git_source, staging, command_runner=command_runner)
        else:
            raise ExtensionInstallError(f"unsupported local extension source: {source}")

        _validate_staged_extension(staging_root)
        _publish_staged_extension(staging, destination, force=force)
        return destination
    except OSError as exc:
        raise ExtensionInstallError(f"extension installation failed: {exc}") from exc
    finally:
        if staging_root is not None:
            with contextlib.suppress(OSError):
                _remove_path(staging_root)


def _local_install_name(source: Path) -> str:
    if source.is_file() and source.suffix != ".py":
        raise ExtensionInstallError("a local extension file must have a .py suffix")
    name = source.name
    validate_install_name(name)
    return name


def _validate_staged_extension(staging_root: Path) -> None:
    paths = RunAgentResourcePaths(root=staging_root, cwd=Path.cwd())
    discovered, diagnostics = discover_extensions(paths)
    errors = [diagnostic.message for diagnostic in diagnostics if diagnostic.severity == "error"]
    if errors:
        raise ExtensionInstallError("invalid extension package: " + "; ".join(errors))
    if not discovered:
        raise ExtensionInstallError(
            "an extension directory must contain extension.py or declare "
            "[tool.run].extensions in pyproject.toml"
        )


def _publish_staged_extension(staging: Path, destination: Path, *, force: bool) -> None:
    if destination.exists() or destination.is_symlink():
        if not force:
            raise ExtensionInstallError(
                f"extension is already installed at {destination}; pass --force to replace it"
            )
        backup = destination.with_name(f".{destination.name}.backup")
        _remove_path(backup)
        destination.replace(backup)
        try:
            staging.replace(destination)
        except BaseException:
            backup.replace(destination)
            raise
        _remove_path(backup)
        return
    staging.replace(destination)


def _local_copy_ignore(_directory: str, names: Sequence[str]) -> set[str]:
    ignored = {".git", ".hg", ".svn", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    return ignored.intersection(names)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
