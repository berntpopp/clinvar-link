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


async def test_meta_carries_freshness(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert isinstance(out["_meta"]["age_days"], int)
    assert isinstance(out["_meta"]["past_ttl"], bool)


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


async def test_search_rejects_negative_offset(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1", "offset": -1})
    assert out["success"] is False and out["error_code"] == "invalid_input"


async def test_search_blank_query_returns_invalid_input(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "  "})
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


async def test_blank_identifier_does_not_echo_blank(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "   "})
    assert out["success"] is False and out["error_code"] == "invalid_input"
    assert out["fallback_args"] in (None, {})  # no blank query echoed
    for cmd in out["_meta"]["next_commands"]:
        assert cmd["arguments"].get("query", "").strip() != "" or "query" not in cmd["arguments"]


@pytest.mark.parametrize(
    ("identifier", "variation_id"),
    [
        ("NM_001267550.2:c.2926dupA", 100010),  # #147: stored as c.2926dup
        ("NM_001267550.2(TTN):c.21229delG", 100006),  # stored as c.21229del
        ("NM_000249.4:c.1852_1854delAAG", 100012),  # stored as c.1852_1854del
    ],
)
async def test_get_variant_resolves_legacy_dup_del_with_trailing_bases(
    mcp, identifier, variation_id
):
    """#147: the older HGVS style repeats the duplicated/deleted bases (dupC, delAG)."""
    out = await call_tool(mcp, "get_variant", {"identifier": identifier})
    assert out["success"] is True
    assert out["variation_id"] == variation_id


async def test_delins_bases_are_never_stripped(mcp):
    """delins/ins bases are the change itself; only a dup/del's repeated bases are redundant."""
    out = await call_tool(mcp, "get_variant", {"identifier": "NM_014855.3:c.80_83delinsTGC"})
    assert out["error_code"] == "not_found"


async def test_combined_aggregate_keeps_clinvar_s_own_term(mcp):
    """#149: 'Pathogenic/Likely pathogenic' folds to likely_pathogenic; the verbatim term rides
    alongside so the record is not understated as plain likely pathogenic."""
    for mode in ("minimal", "compact", "standard"):
        out = await call_tool(
            mcp, "get_variant", {"identifier": "VCV000100012", "response_mode": mode}
        )
        assert out["classification"] == "likely_pathogenic"
        assert out["clinical_significance"] == "Pathogenic/Likely pathogenic"
    single = await call_tool(mcp, "get_variant", {"identifier": "VCV000100005"})
    assert "clinical_significance" not in single


async def test_hgvs_miss_says_the_variant_may_be_on_another_transcript(mcp):
    """#148: a non-preferred-transcript HGVS miss is not evidence that ClinVar has no record."""
    out = await call_tool(mcp, "get_variant", {"identifier": "NM_004992.4:c.473C>T"})
    assert out["error_code"] == "not_found"
    assert "another transcript" in out["recovery"]
    assert "MANE" in out["recovery"]
