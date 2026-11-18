"""End-to-end smoke tests for both transports against the fixture DB.

Two transports share one service surface:

* HTTP (``clinvar-link serve``): a thin FastAPI host exposes ``/health`` and
  mounts the MCP HTTP app. The host is built by ``UnifiedServerManager``.
* stdio (``clinvar-link-mcp`` / ``mcp_server.main``): runs the same MCP facade
  on the FastMCP stdio transport.

Approach for the ASGI app (requirement 1):
    We do NOT drive ``UnifiedServerManager.start_server`` end-to-end because it
    spins up uvicorn, installs signal handlers, and serves forever. Instead we
    mirror exactly what the manager does for the health endpoint: construct the
    same app via the manager's own app-factory coroutine
    (``UnifiedServerManager._create_fastapi_app``), then manually enter the
    FastAPI lifespan (``app.router.lifespan_context(app)``) so that
    ``app.state.clinvar_service`` is populated from ``settings.db_path`` -- the
    identical sequence the manager runs inside ``start_unified_server``. We set
    ``manager.logger`` first (the factory + lifespan log through it) and run
    ``configure_logging`` once. The MCP mount is intentionally omitted: it is
    exercised directly over the facade in the MCP/stdio tests below, and the
    HTTP test only needs ``/health``.

The fixture DB is targeted by monkeypatching ``clinvar_link.config.settings``
(``DATA_DIR`` + ``DB_FILENAME``) so the resolved ``settings.db_path`` property
equals ``built_db``. Both ``server_manager`` and ``mcp_server`` read this same
singleton, so the patch covers both transports. ``monkeypatch`` reverts the
attributes after each test, keeping other tests unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

import mcp_server
from clinvar_link import config
from clinvar_link.config import ServerConfig
from clinvar_link.logging_config import configure_logging
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.server_manager import UnifiedServerManager

# The seven tools that make up the ClinVar Link surface (both transports).
EXPECTED_TOOLS = {
    "get_server_capabilities",
    "get_variant",
    "get_variants",
    "search_variants",
    "get_gene_clinvar_summary",
    "get_variants_by_gene",
    "get_variants_by_pmid",
}


def _point_settings_at(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    """Patch the shared settings singleton so ``db_path`` resolves to ``db``.

    ``settings.db_path`` is the computed property ``DATA_DIR / DB_FILENAME``,
    so patching those two attributes is the supported way to redirect the
    database both ``server_manager`` and ``mcp_server`` open.
    """
    monkeypatch.setattr(config.settings, "DATA_DIR", db.parent, raising=False)
    monkeypatch.setattr(config.settings, "DB_FILENAME", db.name, raising=False)


# --------------------------------------------------------------------------- #
# Requirement 1: HTTP /health over httpx ASGITransport.
# --------------------------------------------------------------------------- #


async def test_http_health_reports_healthy(built_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_settings_at(monkeypatch, built_db)
    assert config.settings.db_path == built_db

    # Mirror the manager: configure logging, attach a logger, build the app via
    # the manager's own factory, then enter the lifespan to populate
    # app.state.clinvar_service (the same compose start_unified_server runs).
    configure_logging("INFO", "console")
    manager = UnifiedServerManager()
    manager.logger = __import__("structlog").get_logger("clinvar_link_test")

    app = await manager._create_fastapi_app(ServerConfig())

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "clinvar_release_date" in body
    # The fixture is built with last_modified "Mon, 01 Jan 2026 ...", so a
    # release date must be present (not None) for the fixture index.
    assert body["clinvar_release_date"] is not None


# --------------------------------------------------------------------------- #
# Requirement 2: MCP tools over the facade (the surface the stdio path serves).
# --------------------------------------------------------------------------- #


async def test_facade_lists_tools_and_get_variant(facade: Any) -> None:
    async with Client(facade) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS

        res = await client.call_tool("get_variant", {"identifier": "VCV000100001"})
        # get_variant declares an output_schema (v1.1 traits[] fencing), so
        # FastMCP's client reconstructs `.data` into a generated pydantic
        # model instead of a plain dict; `.structured_content` is always the
        # raw dict the server emitted.
        data = res.structured_content

    assert data["success"] is True
    assert data["classification"] == "pathogenic"
    assert data["recommended_citation"]


# --------------------------------------------------------------------------- #
# Requirement 4 (optional): MCP resources include clinvar://capabilities.
# --------------------------------------------------------------------------- #


async def test_facade_exposes_capabilities_resource(facade: Any) -> None:
    async with Client(facade) as client:
        resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "clinvar://capabilities" in uris


# --------------------------------------------------------------------------- #
# Requirement 3: stdio entrypoint builds against the fixture DB.
# --------------------------------------------------------------------------- #


async def test_stdio_entrypoint_builds_and_lists_tools(
    built_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_settings_at(monkeypatch, built_db)
    assert config.settings.db_path == built_db

    # _build_mcp() opens settings.db_path (read-only) and builds the facade.
    mcp = mcp_server._build_mcp()
    assert mcp is not None

    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS


# --------------------------------------------------------------------------- #
# Sanity: the facade built directly from the injected service matches too.
# --------------------------------------------------------------------------- #


async def test_facade_from_service_factory(service: Any) -> None:
    mcp = create_clinvar_mcp(service_factory=lambda: service)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


# --------------------------------------------------------------------------- #
# Requirement 3: the 2026-07-28 sessionless protocol era over the real HTTP mount.
# --------------------------------------------------------------------------- #
_MODERN_VERSION = "2026-07-28"
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "clinvar-host-test", "version": "1.0.0"},
}


def _modern_headers(method: str, *, name: str | None = None) -> dict[str, str]:
    headers = {
        "Host": "localhost",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "mcp-protocol-version": _MODERN_VERSION,
        "mcp-method": method,
    }
    if name is not None:
        headers["mcp-name"] = name
    return headers


async def test_modern_mcp_http_discovers_lists_calls_and_fences_unknown_tool(
    built_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """server/discover → tools/list → tools/call over the modern (sessionless) era,
    through the strict Host/Origin guard; an unknown tool name is never reflected."""
    import json

    _point_settings_at(monkeypatch, built_db)
    configure_logging("INFO", "console")
    manager = UnifiedServerManager()
    manager.logger = __import__("structlog").get_logger("clinvar_link_test")
    app = await manager._create_unified_app(ServerConfig(allowed_hosts=["localhost"]))
    hostile = "ignore-instructions-secret"  # ASCII: the modern era mirrors it in mcp-name

    def rpc(rid: int, method: str, **params: Any) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": {"_meta": _MODERN_META, **params},
        }

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            discover = await client.post(
                "/mcp", headers=_modern_headers("server/discover"), json=rpc(1, "server/discover")
            )
            listed = await client.post(
                "/mcp", headers=_modern_headers("tools/list"), json=rpc(2, "tools/list")
            )
            called = await client.post(
                "/mcp",
                headers=_modern_headers("tools/call", name="get_server_capabilities"),
                json=rpc(3, "tools/call", name="get_server_capabilities", arguments={}),
            )
            unknown = await client.post(
                "/mcp",
                headers=_modern_headers("tools/call", name=hostile),
                json=rpc(4, "tools/call", name=hostile, arguments={}),
            )

    assert discover.status_code == 200, discover.text
    assert _MODERN_VERSION in discover.json()["result"]["supportedVersions"]
    assert "mcp-session-id" not in discover.headers
    assert listed.status_code == 200, listed.text
    assert {t["name"] for t in listed.json()["result"]["tools"]} == EXPECTED_TOOLS
    assert called.status_code == 200, called.text
    called_result = called.json()["result"]
    assert called_result["structuredContent"]["success"] is True
    assert unknown.status_code == 200, unknown.text
    unknown_result = unknown.json()["result"]
    assert unknown_result["isError"] is True
    assert json.loads(unknown_result["content"][0]["text"]) == unknown_result["structuredContent"]
    assert unknown_result["structuredContent"]["error_code"] == "not_found"
    assert hostile not in unknown.text
