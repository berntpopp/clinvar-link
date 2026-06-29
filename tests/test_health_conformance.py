"""MCP Transport Standard v1 — /health conformance unit test.

Asserts that the /health endpoint returns all three required keys:
  {status, version, transport}

This is a TDD gate: write it first (RED), then add the missing fields to
the health handler (GREEN). It runs in-process (ASGI transport), so it needs
the fixture DB but no Docker.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import structlog

from clinvar_link import config
from clinvar_link.config import ServerConfig
from clinvar_link.logging_config import configure_logging
from clinvar_link.server_manager import UnifiedServerManager


async def test_health_carries_status_version_transport(
    built_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /health must include status, version, and transport (MCP Standard v1)."""
    monkeypatch.setattr(config.settings, "DATA_DIR", built_db.parent, raising=False)
    monkeypatch.setattr(config.settings, "DB_FILENAME", built_db.name, raising=False)

    configure_logging("INFO", "console")
    manager = UnifiedServerManager()
    manager.logger = structlog.get_logger("clinvar_link_test")

    app = await manager._create_fastapi_app(ServerConfig())

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert "status" in body, f"missing 'status' in /health: {body}"
    assert "version" in body, f"missing 'version' in /health: {body}"
    assert "transport" in body, f"missing 'transport' in /health: {body}"
    assert body["transport"] == "streamable-http-stateless", (
        f"transport must be 'streamable-http-stateless', got {body['transport']!r}"
    )
