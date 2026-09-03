"""Discovery and loading for trusted Python extensions."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Iterable

from .contracts import EXTENSION_API_VERSION, ExtensionScope, ExtensionSpec, SourceInfo


class ExtensionDiscoveryError(RuntimeError):
    pass


def _extension_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ExtensionDiscoveryError(f"extension path does not exist: {path}")
    package = path / "__init__.py"
    if package.is_file():
        return [package.resolve()]
    files = [item for item in path.glob("*.py") if not item.name.startswith("_")]
    files.extend(item / "__init__.py" for item in path.iterdir() if item.is_dir() and (item / "__init__.py").is_file())
    return sorted(set(item.resolve() for item in files))


def _load_module(path: Path) -> ModuleType:
    key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module_name = f"run_agent_extension_{key}"
    package_dir = path.parent if path.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(
        module_name,
        path,
        submodule_search_locations=[str(package_dir)] if package_dir else None,
    )
    if spec is None or spec.loader is None:
        raise ExtensionDiscoveryError(f"cannot create module spec for extension: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_extension_spec(path: str | Path, *, scope: ExtensionScope = "explicit") -> ExtensionSpec:
    resolved = Path(path).expanduser().resolve()
    module = _load_module(resolved)
    declared_api = getattr(module, "EXTENSION_API_VERSION", EXTENSION_API_VERSION)
    if declared_api != EXTENSION_API_VERSION:
        raise ExtensionDiscoveryError(
            f"extension API version {declared_api!r} is incompatible with {EXTENSION_API_VERSION}: {resolved}"
        )
    setup = getattr(module, "setup", None)
    if not callable(setup):
        raise ExtensionDiscoveryError(f"extension must export setup(api): {resolved}")
    default_name = resolved.parent.name if resolved.name == "__init__.py" else resolved.stem
    name = str(getattr(module, "EXTENSION_NAME", default_name)).strip()
    requires_value = getattr(module, "EXTENSION_REQUIRES", ())
    if isinstance(requires_value, str):
        requires = (requires_value,)
    else:
        requires = tuple(str(item) for item in requires_value)
    return ExtensionSpec(
        name=name,
        setup=setup,
        requires=requires,
        source=SourceInfo(name=name, scope=scope, path=resolved),
    )


def discover_extension_specs(
    workspace: str | Path,
    *,
    explicit_paths: Iterable[str | Path] = (),
    load_user: bool = True,
    load_project: bool = False,
) -> tuple[ExtensionSpec, ...]:
    """Discover extensions in deterministic trust and precedence order.

    Project extensions are executable Python and are never loaded unless
    ``load_project`` is explicitly true. Explicit paths are trusted by virtue
    of being supplied by the caller.
    """

    roots: list[tuple[Path, ExtensionScope]] = []
    if load_user:
        roots.append((Path.home() / ".run" / "extensions", "user"))
    if load_project:
        roots.append((Path(workspace).expanduser().resolve() / ".run" / "extensions", "project"))
    roots.extend((Path(path).expanduser(), "explicit") for path in explicit_paths)

    specs: list[ExtensionSpec] = []
    for root, scope in roots:
        if scope != "explicit" and not root.exists():
            continue
        for path in _extension_files(root.resolve()):
            specs.append(load_extension_spec(path, scope=scope))
    return tuple(specs)


__all__ = [
    "ExtensionDiscoveryError",
    "discover_extension_specs",
    "load_extension_spec",
]
