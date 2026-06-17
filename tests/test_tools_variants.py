"""MCP tool tests for the variant tools (get_variant, search_variants)."""

from __future__ import annotations

import pytest

from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool


@pytest.fixture
def mcp(tmp_path):
    """A ClinVar Link MCP facade wired to a fixture-backed service."""
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


async def test_get_variant_by_vcv(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert out["success"] is True
    assert out["classification"] == "pathogenic"
    assert out["recommended_citation"]
    assert out["_meta"]["next_commands"]


async def test_get_variant_by_rsid(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "rs80357906"})
    assert out["success"] is True
    assert out["variation_id"] == 100001


async def test_get_variant_not_found_returns_envelope(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV999999999"})
    assert out["success"] is False
    assert out["error_code"] == "not_found"


async def test_response_mode_minimal_trims_keys(mcp):
    minimal = await call_tool(
        mcp, "get_variant", {"identifier": "VCV000100001", "response_mode": "minimal"}
    )
    full = await call_tool(
        mcp, "get_variant", {"identifier": "VCV000100001", "response_mode": "full"}
    )
    # The minimal projection is a strict subset of full (ignoring the _meta/success
    # envelope keys that run_mcp_tool injects on both).
    envelope_keys = {"_meta", "success"}
    minimal_payload = set(minimal) - envelope_keys
    full_payload = set(full) - envelope_keys
    assert minimal_payload < full_payload
    assert "coordinates" not in minimal and "coordinates" in full


async def test_get_variant_resolves_gene_unqualified_hgvs(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "NM_007294.4:c.5266dupC"})
    assert out["success"] is True
    assert out["variation_id"] == 100001


async def test_get_variants_batch_tool(mcp):
    out = await call_tool(mcp, "get_variants", {"identifiers": ["VCV000100001", "VCV999999999"]})
    assert out["success"] is True
    assert out["requested"] == 2
    assert out["found_count"] == 1
    assert len(out["results"]) == 2
    assert out["_meta"]["next_commands"]
    assert out["_meta"]["request_id"]


async def test_get_variant_garbage_returns_invalid_input(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "@@bad@@"})
    assert out["success"] is False and out["error_code"] == "invalid_input"


async def test_search_rejects_nonpositive_limit(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1", "limit": 0})
    assert out["success"] is False and out["error_code"] == "invalid_input"


async def test_search_variants_returns_results_and_next_commands(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1", "limit": 5})
    assert out["success"] is True
    assert out["count"] >= 1
    assert out["results"]
    assert out["_meta"]["next_commands"]
    # First next_command should chain to get_variant on the top hit.
    tools = [c["tool"] for c in out["_meta"]["next_commands"]]
    assert "get_variant" in tools
