"""Optional Mem0 Platform-backed durable-memory extension."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx

from run_agent_coding.extensions.api import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionHandler,
    SessionShutdownEvent,
)
from run_agent_core.messages import TextContent
from run_agent_core.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from run_agent_core.types import JSONValue

DEFAULT_MEM0_BASE_URL = "https://api.mem0.ai"
DEFAULT_MEM0_USER_ID = "run-agent-user"
DEFAULT_MEM0_APP_ID = "run-agent"


class Mem0Error(RuntimeError):
    """Base error raised by the Mem0 integration."""


class Mem0ConfigurationError(Mem0Error):
    """Raised when the external memory service is not configured correctly."""


@dataclass(frozen=True, slots=True)
class Mem0Config:
    api_key: str
    base_url: str = DEFAULT_MEM0_BASE_URL
    user_id: str = DEFAULT_MEM0_USER_ID
    app_id: str = DEFAULT_MEM0_APP_ID
    timeout_seconds: float = 30.0
    org_id: str | None = None
    project_id: str | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> Mem0Config:
        api_key = environment.get("MEM0_API_KEY", "").strip()
        if not api_key:
            raise Mem0ConfigurationError(
                "Mem0 is not configured; set MEM0_API_KEY in the process environment or .env"
            )
        base_url = environment.get("MEM0_BASE_URL", DEFAULT_MEM0_BASE_URL).strip().rstrip("/")
        parsed_url = httpx.URL(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise Mem0ConfigurationError("MEM0_BASE_URL must be an absolute HTTP(S) URL")
        timeout_seconds = _positive_float(environment.get("MEM0_TIMEOUT_SECONDS", "30"))
        return cls(
            api_key=api_key,
            base_url=base_url,
            user_id=environment.get("MEM0_USER_ID", DEFAULT_MEM0_USER_ID).strip()
            or DEFAULT_MEM0_USER_ID,
            app_id=environment.get("MEM0_APP_ID", DEFAULT_MEM0_APP_ID).strip()
            or DEFAULT_MEM0_APP_ID,
            timeout_seconds=timeout_seconds,
            org_id=environment.get("MEM0_ORG_ID", "").strip() or None,
            project_id=environment.get("MEM0_PROJECT_ID", "").strip() or None,
        )


@dataclass(frozen=True, slots=True)
class Mem0Record:
    id: str
    text: str
    tags: tuple[str, ...] = ()
    created_at: str | None = None
    score: float | None = None


@dataclass(frozen=True, slots=True)
class Mem0WriteResult:
    ids: tuple[str, ...]
    status: str
    event_id: str | None = None


class Mem0MemoryClient:
    """Minimal async client for the current Mem0 Platform memory endpoints."""

    def __init__(
        self,
        config: Mem0Config,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient()

    async def put(
        self,
        *,
        text: str,
        project: Path,
        scope: str,
        tags: tuple[str, ...],
    ) -> Mem0WriteResult:
        agent_id = self._agent_id(project=project, scope=scope)
        payload: dict[str, JSONValue] = {
            "messages": [{"role": "user", "content": text}],
            "user_id": self.config.user_id,
            "agent_id": agent_id,
            "app_id": self.config.app_id,
            "infer": False,
            "metadata": {
                "run_agent_scope": scope,
                "run_agent_project": _project_key(project),
                "tags": list(tags),
            },
        }
        body = await self._request("POST", "/v3/memories/add/", json=payload)
        results = _result_items(body)
        ids = tuple(
            record_id
            for item in results
            if (record_id := _optional_string(item.get("id"))) is not None
        )
        return Mem0WriteResult(
            ids=ids,
            status=_optional_string(body.get("status")) or "SUCCEEDED",
            event_id=_optional_string(body.get("event_id")),
        )

    async def search(self, *, query: str, project: Path, limit: int) -> list[Mem0Record]:
        body = await self._request(
            "POST",
            "/v3/memories/search/",
            json={
                "query": query,
                "filters": self._filters(project),
                "top_k": limit,
                "latest_only": True,
            },
        )
        return _records(body)[:limit]

    async def list(self, *, project: Path, limit: int) -> list[Mem0Record]:
        body = await self._request(
            "POST",
            "/v3/memories/",
            json={"filters": self._filters(project), "latest_only": True},
            params={"page": 1, "page_size": limit},
        )
        return _records(body)[:limit]

    async def delete(self, memory_id: str) -> bool:
        await self._request("DELETE", f"/v1/memories/{quote(memory_id, safe='')}/")
        return True

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    def _filters(self, project: Path) -> dict[str, JSONValue]:
        return {
            "AND": [
                {"user_id": self.config.user_id},
                {"app_id": self.config.app_id},
                {
                    "agent_id": {
                        "in": [
                            self._agent_id(project=project, scope="project"),
                            self._agent_id(project=project, scope="global"),
                        ]
                    }
                },
            ]
        }

    def _agent_id(self, *, project: Path, scope: str) -> str:
        if scope == "global":
            return f"{self.config.app_id}-global"
        if scope != "project":
            raise ValueError("memory scope must be project or global")
        return f"{self.config.app_id}-project-{_project_key(project)}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, JSONValue] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any]:
        query = dict(params or {})
        if self.config.org_id is not None:
            query["org_id"] = self.config.org_id
        if self.config.project_id is not None:
            query["project_id"] = self.config.project_id
        try:
            response = await self._http_client.request(
                method,
                f"{self.config.base_url}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Token {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=json,
                params=query,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_error_detail(exc.response)
            raise Mem0Error(f"Mem0 API returned HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise Mem0Error(f"Mem0 API request failed: {type(exc).__name__}: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise Mem0Error("Mem0 API returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise Mem0Error("Mem0 API returned an invalid response envelope")
        return cast(dict[str, Any], body)


def setup(api: ExtensionAPI) -> None:
    """Register the memory tool against Mem0; local JSON storage is intentionally absent."""
    client: Mem0MemoryClient | None = None
    configuration_error: Mem0ConfigurationError | None = None
    try:
        config = Mem0Config.from_environment(api.context.environment)
    except Mem0ConfigurationError as exc:
        configuration_error = exc
    else:
        client = Mem0MemoryClient(config)

    async def execute(
        tool_call_id: str,
        arguments: dict[str, JSONValue] | Any,
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, on_update
        if signal is not None and signal.is_cancelled():
            return AgentToolResult(content=[TextContent(text="Memory operation cancelled")])
        if client is None:
            assert configuration_error is not None
            raise configuration_error
        action = str(arguments.get("action", "search"))
        project = api.context.cwd.resolve()
        if action == "put":
            text = str(arguments.get("text", "")).strip()
            if not text:
                raise ValueError("memory put requires non-empty text")
            raw_tags = arguments.get("tags", [])
            tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, list) else ()
            scope = str(arguments.get("scope", "project"))
            result = await client.put(text=text, project=project, scope=scope, tags=tags)
            identifier = ", ".join(result.ids) or result.event_id or "accepted"
            return AgentToolResult(
                content=[TextContent(text=f"Stored Mem0 memory {identifier}")],
                details={
                    "action": action,
                    "backend": "mem0",
                    "ids": list(result.ids),
                    "status": result.status,
                    "event_id": result.event_id,
                },
            )
        if action in {"search", "list"}:
            raw_limit = arguments.get("limit", 10)
            parsed_limit = int(raw_limit) if isinstance(raw_limit, str | int | float) else 10
            limit = max(1, min(parsed_limit, 50))
            if action == "search":
                query = str(arguments.get("query", "")).strip()
                if not query:
                    raise ValueError("memory search requires non-empty query")
                records = await client.search(query=query, project=project, limit=limit)
            else:
                records = await client.list(project=project, limit=limit)
            text = "\n".join(
                f"- {record.id}: {record.text}"
                + (f" [{', '.join(record.tags)}]" if record.tags else "")
                for record in records
            )
            return AgentToolResult(
                content=[TextContent(text=text or "No matching memories.")],
                details={"action": action, "backend": "mem0", "count": len(records)},
            )
        if action == "delete":
            memory_id = str(arguments.get("id", "")).strip()
            if not memory_id:
                raise ValueError("memory delete requires an id")
            deleted = await client.delete(memory_id)
            return AgentToolResult(
                content=[TextContent(text=f"Mem0 memory {memory_id} deleted.")],
                details={"action": action, "backend": "mem0", "deleted": deleted},
            )
        raise ValueError("memory action must be put, search, list, or delete")

    async def shutdown(event: SessionShutdownEvent, extension_context: ExtensionContext) -> None:
        del event, extension_context
        if client is not None:
            await client.aclose()

    api.register_tool(
        AgentTool(
            name="memory",
            label="Memory",
            description="Store and retrieve durable project-scoped knowledge in Mem0.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["put", "search", "list", "delete"]},
                    "text": {"type": "string"},
                    "query": {"type": "string"},
                    "id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": "string", "enum": ["project", "global"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["action"],
            },
            execute_fn=execute,
            execution_mode="sequential",
            prompt_snippet="Store or search durable project knowledge with Mem0",
        )
    )
    api.on("session_shutdown", cast(ExtensionHandler, shutdown))


def _project_key(project: Path) -> str:
    return sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:24]


def _records(body: Mapping[str, Any]) -> list[Mem0Record]:
    records: list[Mem0Record] = []
    for item in _result_items(body):
        record_id = _optional_string(item.get("id"))
        text = _optional_string(item.get("memory"))
        if text is None and isinstance(item.get("data"), dict):
            text = _optional_string(item["data"].get("memory"))
        if record_id is None or text is None:
            continue
        metadata = item.get("metadata")
        raw_tags = metadata.get("tags", []) if isinstance(metadata, dict) else []
        tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, list) else ()
        raw_score = item.get("score")
        score = float(raw_score) if isinstance(raw_score, int | float) else None
        records.append(
            Mem0Record(
                id=record_id,
                text=text,
                tags=tags,
                created_at=_optional_string(item.get("created_at")),
                score=score,
            )
        )
    return records


def _result_items(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_results = body.get("results", [])
    if not isinstance(raw_results, list):
        raise Mem0Error("Mem0 API response field 'results' must be an array")
    return [cast(dict[str, Any], item) for item in raw_results if isinstance(item, dict)]


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or response.reason_phrase
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:500]
        details = payload.get("details")
        if isinstance(details, dict):
            message = details.get("message")
            if isinstance(message, str) and message:
                return message[:500]
    return response.reason_phrase


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise Mem0ConfigurationError("MEM0_TIMEOUT_SECONDS must be a positive number") from exc
    if value <= 0:
        raise Mem0ConfigurationError("MEM0_TIMEOUT_SECONDS must be a positive number")
    return value


__all__ = [
    "DEFAULT_MEM0_APP_ID",
    "DEFAULT_MEM0_BASE_URL",
    "DEFAULT_MEM0_USER_ID",
    "Mem0Config",
    "Mem0ConfigurationError",
    "Mem0Error",
    "Mem0MemoryClient",
    "Mem0Record",
    "Mem0WriteResult",
    "setup",
]
