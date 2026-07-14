"""search_variants must not answer a BRCA1 question with OCRL variants (issue #26, D2).

The tool's own description says: "Use this to locate a record when you only have a gene symbol
plus a change or other loose text." Doing precisely that —
`query="BRCA1 pathogenic frameshift exon 11"` — returned `match_mode='or_fallback'`,
`total_count=1000` and a top 5 of OCRL, CANT1, F8, BRCA2, BRCA2. Not one BRCA1 variant. The AND
pass matches nothing (the clinical words are not in the indexed variant name), so it silently
fell back to OR, which matches on noise tokens with no preference for the gene symbol.

The fix: a gene-symbol token in the query is promoted to the gene filter, so the loose text can
only ever narrow WITHIN the gene; and any degradation (OR fallback, gene-only fallback) is
declared in the response instead of being presented as confidently-ranked results.
"""

from __future__ import annotations

import pytest

from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool


@pytest.fixture
def mcp(tmp_path):
    service = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: service)
    service.repo.close()


async def test_the_documented_usage_returns_only_the_asked_for_gene(mcp):
    """The exact reproducer from the audit: loose clinical text plus a gene symbol."""
    out = await call_tool(
        mcp,
        "search_variants",
        {"query": "BRCA1 pathogenic frameshift exon 11", "limit": 5},
    )
    assert out["success"] is True
    rows = out["results"]
    assert rows, "the documented usage returned nothing at all"
    assert {row["gene_symbol"] for row in rows} == {"BRCA1"}


async def test_the_inferred_gene_filter_is_declared_in_the_response(mcp):
    """Inference must be visible: the caller can see WHY these rows came back."""
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1 frameshift exon 11"})
    search = out["_meta"]["search"]
    assert search["gene_symbol_inferred"] == "BRCA1"


async def test_a_degraded_fallback_says_so(mcp):
    """An OR fallback is a low-confidence degradation, not a ranked answer."""
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1 frameshift exon 11"})
    search = out["_meta"]["search"]
    assert search["fallback"] in {"or_fallback", "gene_fallback"}
    assert search["notice"]
    assert out["match_mode"] == search["fallback"]


async def test_an_explicit_gene_symbol_filter_is_never_overridden(mcp):
    """A caller-supplied filter wins over any token in the free text."""
    out = await call_tool(
        mcp, "search_variants", {"query": "BRCA1 pathogenic", "gene_symbol": "TTN"}
    )
    assert out["success"] is True
    assert {row["gene_symbol"] for row in out["results"]} == {"TTN"}
    assert out["_meta"]["search"]["gene_symbol_inferred"] is None


async def test_an_exact_match_is_not_degraded(mcp):
    """A query that matches cleanly reports no fallback and no inference noise."""
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1"})
    assert out["success"] is True
    assert out["match_mode"] in {"and", "or"}
    assert out["_meta"]["search"]["fallback"] is None


async def test_an_unknown_gene_symbol_filter_is_not_a_silent_empty(mcp):
    """An unrecognised gene filter must be an error, not zero rows with success:true."""
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1", "gene_symbol": "NOSUCHGENE"})
    assert out["success"] is False
    assert out["error_code"] == "not_found"
