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
    assert out["total_count"] >= 1
    assert out["recommended_citation"]
    assert out["_meta"]["next_commands"]


async def test_gene_summary_not_found_returns_envelope(mcp):
    out = await call_tool(mcp, "get_gene_clinvar_summary", {"gene_symbol": "NOTAGENE"})
    assert out["success"] is False
    assert out["error_code"] == "not_found"


async def test_variants_by_gene_empty_filter_envelope_success(mcp):
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "min_stars": 5})
    assert out["success"] is True
    assert out["results"] == [] and out["total_count"] == 0


async def test_variants_by_gene_sorted_by_stars_desc(mcp):
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "min_stars": 0})
    assert out["success"] is True
    assert out["total_count"] >= 1
    assert "has_more" in out
    assert out["results"]
    stars = [r["star_rating"] for r in out["results"]]
    assert stars == sorted(stars, reverse=True)
    assert out["_meta"]["next_commands"]
