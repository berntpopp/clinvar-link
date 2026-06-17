"""MCP tool tests for the metadata tool and resources (get_server_capabilities)."""

from __future__ import annotations

import pytest

from clinvar_link.mcp.clinvar_date_cache import (
    get_cached_clinvar_release_date,
    reset_clinvar_date_cache,
)
from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool

_EXPECTED_TOOLS = {
    "get_variant",
    "get_variants",
    "search_variants",
    "get_gene_clinvar_summary",
    "get_variants_by_gene",
    "get_server_capabilities",
}


@pytest.fixture
def mcp(tmp_path):
    """A ClinVar Link MCP facade wired to a fixture-backed service.

    Reset the process-level release-date cache so the capabilities priming
    assertion is deterministic regardless of test ordering.
    """
    reset_clinvar_date_cache()
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()
    reset_clinvar_date_cache()


async def test_capabilities_tools_equal_registered_tools(mcp):
    from fastmcp import Client

    out = await call_tool(mcp, "get_server_capabilities", {})
    async with Client(mcp) as client:
        registered = {t.name for t in await client.list_tools()}
    assert set(out["tools"]) == registered  # equality: no over/under-reporting
    assert registered == _EXPECTED_TOOLS


async def test_capabilities_primes_release_date_cache(mcp):
    assert get_cached_clinvar_release_date() is None
    await call_tool(mcp, "get_server_capabilities", {})
    # The fixture builds with a fixed last_modified, so a release date is present.
    assert get_cached_clinvar_release_date() is not None


async def test_capabilities_release_date_is_populated(mcp):
    out = await call_tool(mcp, "get_server_capabilities", {})
    date = get_cached_clinvar_release_date()
    assert date is not None
    assert out["clinvar_release_date"] == date
    assert "clinvar_release" not in out


async def test_success_envelope_meta_carries_release_and_request_id(mcp):
    # Prime the date cache so provenance can echo the live release.
    await call_tool(mcp, "get_server_capabilities", {})
    date = get_cached_clinvar_release_date()
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    meta = out["_meta"]
    assert "clinvar_release" not in meta
    assert meta["clinvar_release_date"] == date
    # Observability: every response is correlatable and carries a latency hint.
    assert isinstance(meta["request_id"], str) and meta["request_id"]
    assert isinstance(meta["latency_ms"], int | float) and meta["latency_ms"] >= 0


async def test_cold_get_variant_carries_release_without_capabilities(mcp):
    # No get_server_capabilities call first: provenance must STILL echo the live
    # release (primed lazily by the service), never "unknown".
    assert get_cached_clinvar_release_date() is None
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert out["_meta"]["clinvar_release_date"] != "unknown"
    assert "clinvar_release" not in out["_meta"]


async def test_client_supplied_request_id_is_echoed(mcp):
    out = await call_tool(
        mcp, "get_variant", {"identifier": "VCV000100001", "request_id": "req-abc-123"}
    )
    assert out["_meta"]["request_id"] == "req-abc-123"


async def test_meta_carries_server_version(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert isinstance(out["_meta"]["server_version"], str)
    assert out["_meta"]["server_version"]  # non-empty
