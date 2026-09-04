"""Versioned extension host for gateway channel adapters."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Sequence
from inspect import iscoroutinefunction
from pathlib import Path
from types import ModuleType

from run_agent_gateway.gateway import GatewayAdapter

GATEWAY_EXTENSION_API_VERSION = 1


class GatewayExtensionError(RuntimeError):
    pass


class GatewayExtensionAPI:
    """Registration surface passed to trusted ``setup_gateway`` functions."""

    def __init__(self, host: GatewayExtensionHost, extension_name: str) -> None:
        self._host = host
        self._extension_name = extension_name

    @property
    def api_version(self) -> int:
        return GATEWAY_EXTENSION_API_VERSION

    @property
    def extension_name(self) -> str:
        return self._extension_name

    def register_adapter(self, adapter: GatewayAdapter) -> None:
        self._host._register_adapter(adapter, owner=self._extension_name)


class GatewayExtensionHost:
    """Load trusted gateway extensions with per-module atomic registration."""

    def __init__(self) -> None:
        self._adapters: list[GatewayAdapter] = []
        self._adapter_owners: dict[str, str] = {}
        self._modules: list[ModuleType] = []

    @property
    def adapters(self) -> tuple[GatewayAdapter, ...]:
        return tuple(self._adapters)

    @property
    def extension_names(self) -> tuple[str, ...]:
        return tuple(module.__name__ for module in self._modules)

    def load(self, paths: Sequence[str | Path]) -> tuple[GatewayAdapter, ...]:
        for path in _discover_paths(paths):
            self._load_module(path)
        return self.adapters

    def _load_module(self, path: Path) -> None:
        module_name = _module_name(path)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise GatewayExtensionError(f"could not load gateway extension: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        before = len(self._adapters)
        try:
            spec.loader.exec_module(module)
            declared_version = getattr(
                module,
                "GATEWAY_EXTENSION_API_VERSION",
                GATEWAY_EXTENSION_API_VERSION,
            )
            if declared_version != GATEWAY_EXTENSION_API_VERSION:
                raise GatewayExtensionError(
                    f"gateway extension {path} requires API {declared_version}; "
                    f"host provides {GATEWAY_EXTENSION_API_VERSION}"
                )
            setup = getattr(module, "setup_gateway", None)
            if not callable(setup) or iscoroutinefunction(setup):
                raise GatewayExtensionError(
                    f"gateway extension {path} must export synchronous setup_gateway(api)"
                )
            extension_name = str(getattr(module, "GATEWAY_EXTENSION_NAME", path.stem)).strip()
            if not extension_name:
                raise GatewayExtensionError(f"gateway extension {path} has an empty name")
            result = setup(GatewayExtensionAPI(self, extension_name))
            if result is not None:
                raise GatewayExtensionError(
                    f"gateway extension {path} setup_gateway(api) must return None"
                )
            self._modules.append(module)
        except BaseException:
            for adapter in self._adapters[before:]:
                self._adapter_owners.pop(adapter.name, None)
            del self._adapters[before:]
            sys.modules.pop(module_name, None)
            raise

    def _register_adapter(self, adapter: GatewayAdapter, *, owner: str) -> None:
        name = getattr(adapter, "name", None)
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise GatewayExtensionError("gateway adapters require a normalized non-empty name")
        for method_name in ("messages", "send", "close"):
            if not callable(getattr(adapter, method_name, None)):
                raise GatewayExtensionError(
                    f"gateway adapter {name!r} has no callable {method_name} method"
                )
        previous_owner = self._adapter_owners.get(name)
        if previous_owner is not None:
            raise GatewayExtensionError(
                f"gateway adapter {name!r} is already registered by {previous_owner!r}"
            )
        self._adapter_owners[name] = owner
        self._adapters.append(adapter)


def _discover_paths(paths: Sequence[str | Path]) -> tuple[Path, ...]:
    discovered: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.is_file():
            if path.suffix != ".py":
                raise GatewayExtensionError(f"gateway extension must be a Python file: {path}")
            discovered.append(path)
            continue
        if path.is_dir():
            discovered.extend(
                candidate.resolve()
                for candidate in sorted(path.glob("*.py"))
                if not candidate.name.startswith("_")
            )
            continue
        raise GatewayExtensionError(f"gateway extension path does not exist: {path}")
    return tuple(discovered)


def _module_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"run_agent_gateway_extension_{path.stem}_{digest}"


__all__ = [
    "GATEWAY_EXTENSION_API_VERSION",
    "GatewayExtensionAPI",
    "GatewayExtensionError",
    "GatewayExtensionHost",
]
