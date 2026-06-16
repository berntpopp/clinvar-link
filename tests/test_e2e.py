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

# The five tools that make up the ClinVar Link surface (both transports).
EXPECTED_TOOLS = {
    "get_server_capabilities",
    "get_variant",
    "search_variants",
    "get_gene_clinvar_summary",
    "get_variants_by_gene",
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


async def test_facade_lists_five_tools_and_get_variant(facade: Any) -> None:
    async with Client(facade) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS

        res = await client.call_tool("get_variant", {"identifier": "VCV000100001"})
        data = getattr(res, "data", None)
        if data is None:
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
