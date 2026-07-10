"""Host/Origin boundary contracts for the unified MCP application."""

from __future__ import annotations

import asyncio
import inspect
from importlib.metadata import version
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from packaging.version import Version

from clinvar_link.config import ServerConfig, Settings
from clinvar_link.server_manager import UnifiedServerManager


def _build_client() -> TestClient:
    manager = UnifiedServerManager()
    manager.logger = MagicMock()
    config = ServerConfig(
        allowed_hosts=["localhost", "127.0.0.1", "::1", "clinvar-link.genefoundry.org"],
        allowed_origins=["https://genefoundry.org"],
    )
    app = asyncio.run(manager._create_unified_app(config))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client() -> TestClient:
    return _build_client()


def test_fastmcp_344_guard_api_is_installed() -> None:
    assert Version(version("fastmcp")) >= Version("3.4.4")
    parameters = inspect.signature(FastMCP.http_app).parameters
    assert "host_origin_protection" in parameters
    assert "allowed_hosts" in parameters
    assert "allowed_origins" in parameters


@pytest.mark.parametrize(
    "host",
    ["clinvar-link.genefoundry.org", "clinvar-link.genefoundry.org:443"],
)
def test_configured_public_host_is_allowed(client: TestClient, host: str) -> None:
    response = client.get("/mcp", headers={"Host": host})
    assert response.status_code not in {403, 421}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_hosts_are_allowed(client: TestClient, host: str) -> None:
    response = client.get("/mcp", headers={"Host": host})
    assert response.status_code not in {403, 421}


@pytest.mark.parametrize("path", ["/", "/health", "/mcp"])
def test_untrusted_host_is_rejected_on_every_route(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Host": "evil.example"})
    assert response.status_code == 421


def test_absent_and_configured_origins_are_allowed(client: TestClient) -> None:
    no_origin = client.get("/mcp", headers={"Host": "clinvar-link.genefoundry.org"})
    configured_origin = client.get(
        "/mcp",
        headers={
            "Host": "clinvar-link.genefoundry.org",
            "Origin": "https://genefoundry.org",
        },
    )
    assert no_origin.status_code not in {403, 421}
    assert configured_origin.status_code not in {403, 421}


@pytest.mark.parametrize("path", ["/", "/health", "/mcp"])
def test_untrusted_origin_is_rejected_on_every_route(client: TestClient, path: str) -> None:
    response = client.get(
        path,
        headers={
            "Host": "clinvar-link.genefoundry.org",
            "Origin": "https://evil.example",
        },
    )
    assert response.status_code == 403


def test_untrusted_preflight_is_rejected_by_outer_guard(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Host": "evil.example",
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 421


async def test_native_mcp_guard_receives_the_same_strict_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = UnifiedServerManager()
    manager.logger = MagicMock()
    host_app = FastAPI()
    mcp_app = FastAPI()
    mcp = MagicMock()
    mcp.http_app.return_value = mcp_app
    config = ServerConfig(
        allowed_hosts=["localhost", "clinvar-link.genefoundry.org"],
        allowed_origins=["https://genefoundry.org"],
    )

    async def create_host(_config: ServerConfig) -> FastAPI:
        return host_app

    monkeypatch.setattr(manager, "_create_fastapi_app", create_host)
    monkeypatch.setattr(manager, "_create_mcp_server", MagicMock(return_value=mcp))
    monkeypatch.setattr(manager, "_compose_lifespan", MagicMock())

    await manager._create_unified_app(config)

    mcp.http_app.assert_called_once_with(
        path="/mcp",
        stateless_http=True,
        json_response=True,
        host_origin_protection=True,
        allowed_hosts=config.allowed_hosts,
        allowed_origins=config.allowed_origins,
    )


@pytest.mark.parametrize("wildcard", ["*", "*.example.org", "host?.example.org", "host[0]"])
def test_wildcard_host_is_rejected(wildcard: str) -> None:
    with pytest.raises(ValueError, match="wildcard"):
        Settings(_env_file=None, MCP_ALLOWED_HOSTS=[wildcard])


def test_json_environment_allowlists_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CLINVAR_LINK_MCP_ALLOWED_HOSTS",
        '["localhost","clinvar-link.genefoundry.org"]',
    )
    monkeypatch.setenv(
        "CLINVAR_LINK_MCP_ALLOWED_ORIGINS",
        '["https://genefoundry.org"]',
    )

    configured = Settings(_env_file=None)

    assert configured.MCP_ALLOWED_HOSTS == ["localhost", "clinvar-link.genefoundry.org"]
    assert configured.MCP_ALLOWED_ORIGINS == ["https://genefoundry.org"]
