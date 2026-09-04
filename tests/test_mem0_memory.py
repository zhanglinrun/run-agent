from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from extensions.mem0.extension import (
    Mem0Config,
    Mem0ConfigurationError,
    Mem0Error,
    Mem0MemoryClient,
)


def test_mem0_config_from_environment() -> None:
    config = Mem0Config.from_environment(
        {
            "MEM0_API_KEY": "secret",
            "MEM0_BASE_URL": "https://memory.example.test/",
            "MEM0_USER_ID": "alice",
            "MEM0_APP_ID": "run-agent",
            "MEM0_TIMEOUT_SECONDS": "12.5",
            "MEM0_ORG_ID": "org-1",
            "MEM0_PROJECT_ID": "project-1",
        }
    )

    assert config.api_key == "secret"
    assert config.base_url == "https://memory.example.test"
    assert config.user_id == "alice"
    assert config.app_id == "run-agent"
    assert config.timeout_seconds == 12.5
    assert config.org_id == "org-1"
    assert config.project_id == "project-1"


@pytest.mark.parametrize(
    "environment, message",
    [
        ({}, "MEM0_API_KEY"),
        ({"MEM0_API_KEY": "key", "MEM0_BASE_URL": "relative"}, "MEM0_BASE_URL"),
        ({"MEM0_API_KEY": "key", "MEM0_TIMEOUT_SECONDS": "0"}, "MEM0_TIMEOUT_SECONDS"),
    ],
)
def test_mem0_config_rejects_invalid_environment(environment: dict[str, str], message: str) -> None:
    with pytest.raises(Mem0ConfigurationError, match=message):
        Mem0Config.from_environment(environment)


@pytest.mark.anyio
async def test_mem0_client_lists_searches_and_deletes_project_and_global_memory(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v3/memories/":
            return httpx.Response(
                200,
                json={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": "listed-memory",
                            "memory": "Prefer concise Chinese answers",
                            "metadata": {"tags": ["preference"]},
                            "created_at": "2026-09-04T00:00:00Z",
                        }
                    ],
                },
            )
        if request.url.path == "/v3/memories/search/":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "searched-memory",
                            "memory": "Use uv",
                            "score": 0.9,
                            "created_at": "2026-09-04T00:00:00Z",
                        }
                    ]
                },
            )
        if request.method == "DELETE" and request.url.raw_path.startswith(b"/v1/memories/a%2Fb/"):
            return httpx.Response(200, json={"message": "Memory deleted successfully!"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = Mem0MemoryClient(
            Mem0Config(
                api_key="secret",
                base_url="https://memory.example.test",
                user_id="alice",
                app_id="run-agent",
                org_id="org-1",
                project_id="project-1",
            ),
            http_client,
        )
        listed = await client.list(project=tmp_path, limit=5)
        searched = await client.search(query="package manager", project=tmp_path, limit=3)
        deleted = await client.delete("a/b")

    assert listed[0].tags == ("preference",)
    assert searched[0].score == 0.9
    assert deleted is True
    for request in requests[:2]:
        payload = json.loads(request.content)
        filters = payload["filters"]["AND"]
        assert filters[0] == {"user_id": "alice"}
        assert filters[1] == {"app_id": "run-agent"}
        assert filters[2]["agent_id"]["in"][1] == "run-agent-global"
        assert request.url.params["org_id"] == "org-1"
        assert request.url.params["project_id"] == "project-1"


@pytest.mark.anyio
async def test_mem0_client_surfaces_sanitized_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"detail": "invalid API key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = Mem0MemoryClient(Mem0Config(api_key="secret"), http_client)
        with pytest.raises(Mem0Error, match="HTTP 401: invalid API key"):
            await client.search(query="query", project=Path.cwd(), limit=5)
