"""MCP tool tests for the gene tools (get_gene_clinvar_summary, get_variants_by_gene)."""

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


async def test_gene_summary(mcp):
    out = await call_tool(mcp, "get_gene_clinvar_summary", {"gene_symbol": "BRCA1"})
    assert out["success"] is True
    # `variant_count`, NOT `total_count`: the fleet's list tools use `total_count` for a PAGE's
    # result-set size, and this payload also carries a truncated top_traits list — one key
    # meaning two things is how a model concludes it is reading page 1 of 15,947 traits.
    assert out["variant_count"] >= 1
    assert "total_count" not in out
    assert out["recommended_citation"]
    assert out["_meta"]["next_commands"]


async def test_gene_summary_not_found_returns_envelope(mcp):
    out = await call_tool(mcp, "get_gene_clinvar_summary", {"gene_symbol": "NOTAGENE"})
    assert out["success"] is False
    assert out["error_code"] == "not_found"


async def test_variants_by_gene_true_zero_is_still_a_success(mcp):
    """A VALID filter that legitimately excludes everything is an empty success, not an error.

    The distinction the silent-empty fix rests on: an UNRECOGNIZED value is invalid_input, but a
    recognized one that matches no row is a truthful zero (BRCA1 has no `not_provided` variant in
    the fixture) and must stay a success.
    """
    out = await call_tool(
        mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "classification": "not_provided"}
    )
    assert out["success"] is True
    assert out["results"] == [] and out["total_count"] == 0


async def test_variants_by_gene_out_of_range_min_stars_is_rejected(mcp):
    """min_stars=5 cannot exist (stars are 0-4) — it used to return an empty success."""
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "min_stars": 5})
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert "min_stars" in out["message"]


async def test_variants_by_gene_sorted_by_stars_desc(mcp):
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "min_stars": 0})
    assert out["success"] is True
    assert out["total_count"] >= 1
    assert "has_more" in out
    assert out["results"]
    stars = [r["star_rating"] for r in out["results"]]
    assert stars == sorted(stars, reverse=True)
    assert out["_meta"]["next_commands"]


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"offset": -1}])
async def test_variants_by_gene_rejects_invalid_pagination(mcp, arguments):
    out = await call_tool(
        mcp,
        "get_variants_by_gene",
        {"gene_symbol": "BRCA1", **arguments},
    )
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"


async def test_gene_not_found_recovery_is_gene_specific(mcp):
    out = await call_tool(mcp, "get_gene_clinvar_summary", {"gene_symbol": "NOSUCHGENE"})
    assert out["success"] is False and out["error_code"] == "not_found"
    assert "VCV" not in out["recovery"] and "rsID" not in out["recovery"]
    assert "gene symbol" in out["recovery"].lower()
