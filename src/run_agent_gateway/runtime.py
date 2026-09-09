"""Gateway host ownership of the shared Coding application and SQLite state."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from run_agent_coding.application import ApplicationOptions, CodingApplication
from run_agent_coding.paths import RunAgentPaths
from run_agent_coding.project_trust import TrustDefault
from run_agent_coding.provider_config import ProviderSettings, load_provider_settings
from run_agent_coding.session import CodingSession
from run_agent_coding.session_manager import SessionManager
from run_agent_coding.thinking import ThinkingLevel


class CodingSessionPool:
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
        self.manager = SessionManager(self.paths)
        self.settings = provider_settings or load_provider_settings(self.paths)
        self.options = ApplicationOptions(
            cwd=self.cwd,
            paths=self.paths,
            provider_name=provider_name,
            model=model,
            thinking=thinking_level_override,
            extension_paths=extension_paths,
            project_extensions_enabled=project_extensions_enabled,
            trust_default=trust_default,
        )
        self._applications: dict[str, CodingApplication] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def resolve(self, gateway_session_id: str) -> CodingSession:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Coding session pool is closed")
            existing = self._applications.get(gateway_session_id)
            if existing is not None:
                return existing.session
            from dataclasses import replace

            identity = _durable_session_id(gateway_session_id)
            record = await self.manager.get_session(identity)
            options = replace(
                self.options,
                resume=identity if record else None,
                session_id=None if record else identity,
            )
            application = await CodingApplication.open(
                options, manager=self.manager, settings=self.settings
            )
            try:
                await application.start()
            except BaseException:
                await application.aclose()
                raise
            self._applications[gateway_session_id] = application
            return application.session

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            applications = tuple(self._applications.values())
            self._applications.clear()
        try:
            results = await asyncio.gather(
                *(item.aclose() for item in applications), return_exceptions=True
            )
            error = next((item for item in results if isinstance(item, BaseException)), None)
            if error is not None:
                raise error
        finally:
            await self.manager.aclose()


def _durable_session_id(gateway_session_id: str) -> str:
    return "gateway-" + hashlib.sha256(gateway_session_id.encode("utf-8")).hexdigest()[:24]
