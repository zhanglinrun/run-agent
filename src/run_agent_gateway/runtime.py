"""Persistent CodingSession pool used by long-running gateway hosts."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from run_agent_coding.extensions import StderrUiBridge
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault
from run_agent_coding.provider_config import (
    ProviderSettings,
    load_provider_settings,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from run_agent_coding.provider_runtime import create_model_provider
from run_agent_coding.session import CodingSession, CodingSessionConfig, jsonl_session_storage
from run_agent_coding.shell_config import load_shell_settings
from run_agent_coding.thinking import ThinkingLevel


class CodingSessionPool:
    """Resolve one durable, serialized CodingSession for each gateway session key."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        provider_name: str | None = None,
        model: str | None = None,
        paths: RunAgentPaths | None = None,
        provider_settings: ProviderSettings | None = None,
        thinking_level_override: ThinkingLevel | None = None,
        extension_paths: tuple[Path, ...] = (),
        project_extensions_enabled: bool = False,
        trust_default: TrustDefault = "never",
    ) -> None:
        self.cwd = Path(cwd).resolve()
        self.paths = paths or RunAgentPaths()
        self.provider_name = provider_name
        self.model = model
        self.provider_settings = provider_settings or load_provider_settings(self.paths)
        self.thinking_level_override = thinking_level_override
        self.extension_paths = extension_paths
        self.project_extensions_enabled = project_extensions_enabled
        self.trust_default = trust_default
        self._sessions: dict[str, CodingSession] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def resolve(self, gateway_session_id: str) -> CodingSession:
        if self._closed:
            raise RuntimeError("coding session pool is closed")
        existing = self._sessions.get(gateway_session_id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._sessions.get(gateway_session_id)
            if existing is not None:
                return existing
            session = await self._create(gateway_session_id)
            self._sessions[gateway_session_id] = session
            return session

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        results = await asyncio.gather(
            *(session.aclose() for session in sessions), return_exceptions=True
        )
        error = next((result for result in results if isinstance(result, BaseException)), None)
        if error is not None:
            raise error

    async def _create(self, gateway_session_id: str) -> CodingSession:
        selection = resolve_provider_selection(
            self.provider_settings,
            provider_name=self.provider_name,
            model=self.model,
        )
        thinking_level = resolve_startup_thinking_level(
            selection.provider,
            selection.model,
            cli_override=self.thinking_level_override,
        )
        provider = create_model_provider(
            selection.provider,
            model=selection.model,
            thinking_level=thinking_level,
        )
        durable_id = _durable_session_id(gateway_session_id)
        storage_path = self.paths.sessions_dir / "gateway" / f"{durable_id}.jsonl"
        shell = load_shell_settings(self.paths)
        try:
            session = await CodingSession.load(
                CodingSessionConfig(
                    provider=provider,
                    owns_initial_provider=True,
                    model=selection.model,
                    thinking_level=thinking_level or "off",
                    storage=jsonl_session_storage(storage_path),
                    cwd=self.cwd,
                    session_id=durable_id,
                    provider_name=selection.provider.name,
                    provider_settings=self.provider_settings,
                    runtime_provider_config=selection.provider,
                    shell_command_prefix=shell.shell_command_prefix,
                    extension_paths=self.extension_paths,
                    project_extensions_enabled=self.project_extensions_enabled,
                    trust_default=self.trust_default,
                )
            )
        except BaseException:
            await provider.aclose()
            raise
        session.extension_runtime.set_ui_bridge(StderrUiBridge())
        return session


def _durable_session_id(gateway_session_id: str) -> str:
    digest = hashlib.sha256(gateway_session_id.encode("utf-8")).hexdigest()[:24]
    return f"gateway-{digest}"


__all__ = ["CodingSessionPool"]
